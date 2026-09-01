"""Paint Layer 데이터를 Blender Image와 Shader Node Graph에 동기화합니다."""

from __future__ import annotations

from math import pow
from pathlib import Path
from typing import Any

import bpy

from .ownership import (
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    SCHEMA_KEY,
    UUID_KEY,
    new_uuid,
)
LAYER_UUID_KEY = "pp_layer_uuid"
ADJUSTMENT_UUID_KEY = "pp_adjustment_uuid"
MASK_UUID_KEY = "pp_mask_uuid"
TARGET_UUID_KEY = "pp_target_uuid"
LAYER_PREVIEW_UUID_KEY = "pp_layer_preview_uuid"
MASK_PREVIEW_UUID_KEY = "pp_mask_preview_uuid"
ORIGINAL_PREVIEW_KEY = "pp_original_preview"
LAYER_IMAGE_ROLE = "OVERLAY_PERSISTENT"
MASK_IMAGE_ROLE = "MASK_PERSISTENT"
LAYER_FRAME_ROLE = "LAYER_FRAME"
LAYER_IMAGE_NODE_ROLE = "OVERLAY_IMAGE_NODE"
LAYER_MIX_NODE_ROLE = "OVERLAY_MIX_NODE"
LAYER_SOLO_PREVIEW_MIX_NODE_ROLE = "LAYER_SOLO_PREVIEW_MIX_NODE"
COMPOSITE_UNPREMULTIPLY_SEPARATE_NODE_ROLE = "COMPOSITE_UNPREMULTIPLY_SEPARATE_NODE"
COMPOSITE_UNPREMULTIPLY_ALPHA_NODE_ROLE = "COMPOSITE_UNPREMULTIPLY_ALPHA_NODE"
COMPOSITE_UNPREMULTIPLY_COMBINE_NODE_ROLE = "COMPOSITE_UNPREMULTIPLY_COMBINE_NODE"
COMPOSITE_UNPREMULTIPLY_RED_DIVIDE_NODE_ROLE = "COMPOSITE_UNPREMULTIPLY_RED_DIVIDE_NODE"
COMPOSITE_UNPREMULTIPLY_GREEN_DIVIDE_NODE_ROLE = "COMPOSITE_UNPREMULTIPLY_GREEN_DIVIDE_NODE"
COMPOSITE_UNPREMULTIPLY_BLUE_DIVIDE_NODE_ROLE = "COMPOSITE_UNPREMULTIPLY_BLUE_DIVIDE_NODE"
ADJUSTMENT_NODE_ROLE = "LAYER_ADJUSTMENT_NODE"
MASK_IMAGE_NODE_ROLE = "LAYER_MASK_IMAGE_NODE"
MASK_BW_NODE_ROLE = "LAYER_MASK_BW_NODE"
MASK_MULTIPLY_NODE_ROLE = "LAYER_MASK_MULTIPLY_NODE"
ADJUSTMENT_APPLY_MIX_NODE_ROLE = "ADJUSTMENT_APPLY_MIX_NODE"
ADJUSTMENT_MASK_IMAGE_NODE_ROLE = "ADJUSTMENT_MASK_IMAGE_NODE"
ADJUSTMENT_MASK_BW_NODE_ROLE = "ADJUSTMENT_MASK_BW_NODE"
ORIGINAL_GATE_NODE_ROLE = "ORIGINAL_BASE_GATE"
COMPOSITE_LAYER_ROLE = "COMPOSITE"
NORMAL_LAYER_ROLE = "NORMAL"
ORIGINAL_LAYER_SCHEMA_VERSION = 2
ORIGINAL_SOURCE_NODE_UUID_KEY = "pp_original_source_node_uuid"
ORIGINAL_LAYER_IMAGE_KEY = "pp_is_original_layer"
ORIGINAL_REFERENCE_PENDING_DELETE_KEY = "pp_original_reference_pending_delete"
COMBINED_RGBA_IMAGE_KEY = "pp_combined_rgba_premultiplied"

_MANAGED_NODE_ROLES = {
    LAYER_FRAME_ROLE,
    LAYER_IMAGE_NODE_ROLE,
    LAYER_MIX_NODE_ROLE,
    LAYER_SOLO_PREVIEW_MIX_NODE_ROLE,
    COMPOSITE_UNPREMULTIPLY_SEPARATE_NODE_ROLE,
    COMPOSITE_UNPREMULTIPLY_ALPHA_NODE_ROLE,
    COMPOSITE_UNPREMULTIPLY_COMBINE_NODE_ROLE,
    COMPOSITE_UNPREMULTIPLY_RED_DIVIDE_NODE_ROLE,
    COMPOSITE_UNPREMULTIPLY_GREEN_DIVIDE_NODE_ROLE,
    COMPOSITE_UNPREMULTIPLY_BLUE_DIVIDE_NODE_ROLE,
    ADJUSTMENT_NODE_ROLE,
    MASK_IMAGE_NODE_ROLE,
    MASK_BW_NODE_ROLE,
    MASK_MULTIPLY_NODE_ROLE,
    ADJUSTMENT_APPLY_MIX_NODE_ROLE,
    ADJUSTMENT_MASK_IMAGE_NODE_ROLE,
    ADJUSTMENT_MASK_BW_NODE_ROLE,
    ORIGINAL_GATE_NODE_ROLE,
}
_NODE_CACHE: dict[tuple[int, str, str], Any] = {}


class LayerShaderError(RuntimeError):
    """Paint Layer Image 또는 Shader Graph 계약 위반을 나타냅니다."""


def _pointer(value: Any) -> int:
    """삭제되었거나 무효한 Blender RNA Pointer를 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _settings(scene: Any):
    """등록된 Projection Paint Scene 설정을 반환합니다."""
    return getattr(scene, "projection_paint", None)


def _owned_node(node: Any, role: str | None = None) -> bool:
    """Node가 요청한 애드온 소유 역할인지 확인합니다."""
    try:
        return bool(
            node.get(OWNER_KEY) == OWNER_TOKEN
            and (
                role is None
                or str(node.get(RECORD_KIND_KEY, "")) == str(role)
            )
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _owned_image(image: Any, roles: set[str] | None = None) -> bool:
    """Image가 요청한 애드온 소유 Paint 역할인지 확인합니다."""
    try:
        role = str(image.get(RECORD_KIND_KEY, ""))
        return bool(
            _pointer(image)
            and image.get(OWNER_KEY) == OWNER_TOKEN
            and str(image.get(UUID_KEY, ""))
            and (roles is None or role in roles)
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _tag_node(
    node: Any,
    role: str,
    layer_uuid: str,
    adjustment_uuid: str = "",
    mask_uuid: str = "",
) -> None:
    """관리 Node에 역할과 Layer·Adjustment·Mask UUID를 기록합니다."""
    node[OWNER_KEY] = OWNER_TOKEN
    node[RECORD_KIND_KEY] = str(role)
    node[SCHEMA_KEY] = 1
    node[LAYER_UUID_KEY] = str(layer_uuid)
    if adjustment_uuid:
        node[ADJUSTMENT_UUID_KEY] = str(adjustment_uuid)
    elif ADJUSTMENT_UUID_KEY in node:
        del node[ADJUSTMENT_UUID_KEY]
    if mask_uuid:
        node[MASK_UUID_KEY] = str(mask_uuid)
    elif MASK_UUID_KEY in node:
        del node[MASK_UUID_KEY]


def _tag_image(
    image: Any,
    role: str,
    target_uuid: str,
    layer_uuid: str,
    adjustment_uuid: str = "",
    mask_uuid: str = "",
) -> None:
    """관리 Image에 역할과 Layer·Adjustment·Mask UUID를 기록합니다."""
    image[OWNER_KEY] = OWNER_TOKEN
    image[RECORD_KIND_KEY] = str(role)
    image[SCHEMA_KEY] = 1
    if not str(image.get(UUID_KEY, "")):
        image[UUID_KEY] = new_uuid()
    image[TARGET_UUID_KEY] = str(target_uuid)
    image[LAYER_UUID_KEY] = str(layer_uuid)
    if adjustment_uuid:
        image[ADJUSTMENT_UUID_KEY] = str(adjustment_uuid)
    elif ADJUSTMENT_UUID_KEY in image:
        del image[ADJUSTMENT_UUID_KEY]
    if mask_uuid:
        image[MASK_UUID_KEY] = str(mask_uuid)
    elif MASK_UUID_KEY in image:
        del image[MASK_UUID_KEY]


def _target_uuid(target: Any) -> str:
    """Paint Target의 기존 UUID를 검증하거나 새로 생성합니다."""
    value = str(target.get(TARGET_UUID_KEY, "") or "")
    if not value:
        value = new_uuid()
        target[TARGET_UUID_KEY] = value
    return value


def _image_size(image: Any) -> tuple[int, int]:
    """유효한 Paint Image 해상도를 정수 쌍으로 반환합니다."""
    if image is None or _pointer(image) == 0:
        raise LayerShaderError("Layer 해상도의 기준 Paint Image가 없습니다.")
    size = tuple(int(value) for value in image.size)
    if len(size) != 2 or min(size) <= 0:
        raise LayerShaderError("Layer Paint Image 해상도가 유효하지 않습니다.")
    return size


def _external_image_paths(image: Any) -> tuple[Path, ...]:
    """외부 FILE·TILED Image가 참조하는 실제 파일 경로를 반환합니다."""
    if image is None or _pointer(image) == 0:
        return ()
    source = str(getattr(image, "source", "") or "")
    if source not in {"FILE", "TILED"}:
        return ()
    raw_path = str(
        getattr(image, "filepath_raw", "")
        or getattr(image, "filepath", "")
        or ""
    )
    if not raw_path:
        return ()
    absolute = str(bpy.path.abspath(raw_path))
    if source == "FILE":
        return (Path(absolute).resolve(),)
    paths = []
    for tile in tuple(getattr(image, "tiles", ())):
        number = int(tile.number)
        u = (number - 1001) % 10 + 1
        v = (number - 1001) // 10 + 1
        resolved = absolute.replace("<UDIM>", str(number)).replace(
            "<UVTILE>",
            f"u{u}_v{v}",
        )
        paths.append(Path(resolved).resolve())
    return tuple(paths)


def _external_original_image(image: Any, *, require_files: bool = True) -> Any | None:
    """복제 없이 사용할 외부 Original Image를 검증하고 필요하면 다시 로드합니다."""
    paths = _external_image_paths(image)
    if not paths:
        return None
    missing = tuple(path for path in paths if not path.is_file())
    if missing and require_files:
        preview = ", ".join(str(path) for path in missing[:3])
        raise LayerShaderError(f"Original Source Texture 파일을 찾을 수 없습니다: {preview}")
    if missing:
        return None
    if bool(getattr(image, "is_dirty", False)):
        raise LayerShaderError(
            "Original Source Texture에 저장되지 않은 변경이 있습니다. "
            "원본 파일을 먼저 저장하거나 다시 불러오십시오."
        )
    if not bool(getattr(image, "has_data", False)):
        try:
            image.reload()
        except RuntimeError as exc:
            raise LayerShaderError(
                f"Original Source Texture를 다시 불러오지 못했습니다: {exc}"
            ) from exc
    return image


def original_layer_is_read_only(settings: Any, layer: Any) -> bool:
    """Layer가 외부 원본 파일을 직접 참조하는 읽기 전용 Original인지 반환합니다."""
    if not bool(getattr(layer, "is_original_layer", False)):
        return False
    source = getattr(settings, "original_source_image", None)
    return bool(
        _pointer(source)
        and _pointer(getattr(layer, "image", None)) == _pointer(source)
        and _external_image_paths(source)
    )


def _managed_layer_reference(
    settings: Any,
    *,
    exclude_layer_uuid: str = "",
) -> Any | None:
    """해상도·Mask·Bake 기준으로 사용할 애드온 소유 Layer Image를 반환합니다."""
    reference = next(
        (
            layer.image
            for layer in settings.layers
            if str(layer.layer_uuid) != str(exclude_layer_uuid)
            and _owned_image(layer.image, {LAYER_IMAGE_ROLE})
        ),
        None,
    )
    if reference is not None:
        return reference
    overlay = getattr(settings, "overlay_image", None)
    return overlay if _owned_image(overlay, {LAYER_IMAGE_ROLE}) else None


def _new_image(
    target: Any,
    name: str,
    size: tuple[int, int],
    *,
    role: str,
    layer_uuid: str,
    adjustment_uuid: str = "",
    mask_uuid: str = "",
    color: tuple[float, float, float, float],
    reference: Any = None,
) -> Any:
    """Target 백엔드와 타일 계약으로 Layer 또는 Mask 관리 Image를 생성합니다."""
    from .image_backend import backend_from_target, new_managed_paint_image
    from .udim_setup import UDIM_TILES_KEY

    tile_numbers = tuple(int(number) for number in target.get(UDIM_TILES_KEY, (1001,)))
    try:
        backend = backend_from_target(target, reference)
        image = new_managed_paint_image(backend, name, size, tile_numbers, color)
    except Exception as exc:
        raise LayerShaderError(f"관리 Paint Image를 생성하지 못했습니다: {exc}") from exc
    _tag_image(
        image,
        role,
        _target_uuid(target),
        layer_uuid,
        adjustment_uuid,
        mask_uuid,
    )
    try:
        image.colorspace_settings.name = (
            "Non-Color" if role == MASK_IMAGE_ROLE else "sRGB"
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    try:
        image.pack()
    except RuntimeError:
        bpy.data.images.remove(image)
        raise
    from .overlay_durability import register_overlay

    register_overlay(image)
    return image


def _target_tile_numbers(target: Any) -> tuple[int, ...]:
    """Paint Target이 요구하는 정렬된 UDIM 타일 번호를 반환합니다."""
    from .udim_setup import UDIM_TILES_KEY

    numbers = tuple(sorted(int(number) for number in target.get(UDIM_TILES_KEY, (1001,))))
    if not numbers or numbers[0] != 1001 or len(numbers) != len(set(numbers)):
        raise LayerShaderError("Paint Target UDIM 타일 계약이 유효하지 않습니다.")
    return numbers


def _image_tile_numbers(image: Any) -> tuple[int, ...]:
    """일반 또는 Tiled Image의 정렬된 논리 타일 번호를 반환합니다."""
    if image is None or _pointer(image) == 0:
        raise LayerShaderError("원본 Image가 유효하지 않습니다.")
    if str(getattr(image, "source", "")) != "TILED":
        return (1001,)
    numbers = tuple(sorted(int(tile.number) for tile in image.tiles))
    if not numbers or numbers[0] != 1001 or len(numbers) != len(set(numbers)):
        raise LayerShaderError("원본 UDIM 타일 계약이 유효하지 않습니다.")
    return numbers


def _target_image_contract(
    target: Any,
    size: tuple[int, int],
    reference: Any = None,
):
    """Target과 기준 Image에서 기대 Paint Image 계약을 구성합니다."""
    from .image_backend import PaintImageContract, backend_from_target

    try:
        backend = backend_from_target(target, reference)
        numbers = _target_tile_numbers(target)
        return PaintImageContract(
            backend,
            numbers,
            tuple((int(size[0]), int(size[1])) for _number in numbers),
        )
    except Exception as exc:
        raise LayerShaderError(f"Paint Target Image 계약이 유효하지 않습니다: {exc}") from exc


def _validate_target_image_contract(
    target: Any,
    image: Any,
    size: tuple[int, int],
    reference: Any = None,
) -> None:
    """Image가 Target의 백엔드·타일·타일별 해상도 계약과 같은지 검증합니다."""
    from .image_backend import validate_image_contract

    expected = _target_image_contract(target, size, reference)
    try:
        validate_image_contract(image, expected)
    except Exception as exc:
        raise LayerShaderError(f"관리 Paint Image 계약이 다릅니다: {exc}") from exc


def _replace_image_identity(image: Any) -> None:
    """복제 Image의 기존 소유 태그와 UUID를 제거해 새 관리 자원으로 준비합니다."""
    for key in (
        OWNER_KEY,
        RECORD_KIND_KEY,
        SCHEMA_KEY,
        UUID_KEY,
        TARGET_UUID_KEY,
        LAYER_UUID_KEY,
        ADJUSTMENT_UUID_KEY,
        MASK_UUID_KEY,
    ):
        if key in image:
            del image[key]


def _match_original_image_contract(image: Any, reference: Any) -> None:
    """새 관리 Original Image에 현재 Stack의 색 공간과 Alpha 계약을 적용합니다."""
    if reference is None or _pointer(reference) == 0:
        return
    image.colorspace_settings.name = str(reference.colorspace_settings.name)
    image.alpha_mode = str(reference.alpha_mode)


def _create_original_image(
    target: Any,
    layer_uuid: str,
    name: str,
    size: tuple[int, int],
    source_image: Any | None,
    color: tuple[float, float, float, float],
    reference: Any = None,
    *,
    scene: Any | None = None,
    material: Any | None = None,
    source_socket: Any | None = None,
) -> Any:
    """외부 원본은 직접 반환하고 생성·Packed·Material 원본만 관리 Image로 만듭니다."""
    external = _external_original_image(source_image, require_files=True)
    if external is not None:
        return external
    has_packed_data = bool(
        source_image is not None
        and (
            getattr(source_image, "packed_file", None) is not None
            or len(tuple(getattr(source_image, "packed_files", ()))) > 0
        )
    )
    if source_image is not None and (
        str(getattr(source_image, "source", "")) == "GENERATED"
        or has_packed_data
    ):
        image = _new_image(
            target,
            name,
            size,
            role=LAYER_IMAGE_ROLE,
            layer_uuid=layer_uuid,
            color=color,
            reference=reference,
        )
        temporary = source_image.copy()
        try:
            _match_original_image_contract(image, reference)
            if _image_size(temporary) != size:
                temporary.scale(int(size[0]), int(size[1]))
            image.pixels.foreach_set(temporary.pixels[:])
            from .overlay_durability import ensure_overlay_durable

            ensure_overlay_durable(image, scene=scene, force=True)
            return image
        except Exception:
            _remove_image(image)
            raise
        finally:
            if _pointer(temporary) and int(temporary.users) == 0:
                _remove_image(temporary)
    image = _new_image(
        target,
        name,
        size,
        role=LAYER_IMAGE_ROLE,
        layer_uuid=layer_uuid,
        color=color,
        reference=reference,
    )
    try:
        _match_original_image_contract(image, reference)
        if source_socket is not None:
            _bake_original_socket_to_image(
                scene,
                material,
                source_socket,
                color,
                image,
            )
            from .overlay_durability import ensure_overlay_durable

            ensure_overlay_durable(image, scene=scene, force=True)
        elif source_image is not None:
            raise LayerShaderError(
                "파일 경로가 없는 Original Image는 안전한 Blender 베이크 컨텍스트가 필요합니다."
            )
        return image
    except Exception:
        _remove_image(image)
        raise


def _bake_original_socket_to_image(
    scene: Any,
    material: Any,
    source_socket: Any,
    default: tuple[float, float, float, float],
    image: Any,
) -> None:
    """지원 가능한 기존 Base Color Socket을 Blender Emission Bake로 관리 Image에 기록합니다."""
    if scene is None or material is None or material.node_tree is None:
        raise LayerShaderError("Original Material Socket Bake에 필요한 Scene과 Material이 없습니다.")
    tree = material.node_tree
    base = _principled(material).inputs["Base Color"]
    if len(base.links) > 1:
        raise LayerShaderError("Original Material Socket Bake 전 Base Color 연결이 둘 이상입니다.")
    previous_source = base.links[0].from_socket if base.links else None
    previous_default = tuple(float(value) for value in base.default_value)
    try:
        _clear_input(tree, base)
        base.default_value = default
        _connect(tree, source_socket, base)
        from .composite_layer import _bake_final_base_color

        _bake_final_base_color(bpy.context, image)
    finally:
        _clear_input(tree, base)
        base.default_value = previous_default
        if previous_source is not None:
            _connect(tree, previous_source, base)


def _tag_original_source_socket(source_socket: Any) -> tuple[str, str]:
    """Restore에서 재사용할 기존 Material Socket의 안정 UUID와 출력 식별자를 기록합니다."""
    node = getattr(source_socket, "node", None)
    identifier = str(getattr(source_socket, "identifier", "") or "")
    if _pointer(node) == 0 or not identifier:
        raise LayerShaderError("Original Material Socket 복원 식별자를 기록할 수 없습니다.")
    token = str(node.get(ORIGINAL_SOURCE_NODE_UUID_KEY, "") or "")
    if not token:
        token = new_uuid()
        node[ORIGINAL_SOURCE_NODE_UUID_KEY] = token
    return token, identifier


def _original_source_socket(material: Any, settings: Any) -> Any | None:
    """저장한 Material Socket provenance가 현재에도 유효한지 엄격하게 확인합니다."""
    token = str(getattr(settings, "original_source_node_uuid", "") or "")
    identifier = str(
        getattr(settings, "original_source_socket_identifier", "") or ""
    )
    if not token and not identifier:
        return None
    if not token or not identifier or material is None or material.node_tree is None:
        raise LayerShaderError("저장된 Original Material Socket provenance가 유효하지 않습니다.")
    nodes = tuple(
        node
        for node in material.node_tree.nodes
        if str(node.get(ORIGINAL_SOURCE_NODE_UUID_KEY, "") or "") == token
    )
    if len(nodes) != 1:
        raise LayerShaderError("Original Material Socket provenance Node를 하나로 찾을 수 없습니다.")
    socket = nodes[0].outputs.get(identifier)
    if socket is None:
        raise LayerShaderError("Original Material Socket provenance 출력이 변경되었습니다.")
    return socket


def _ensure_uuid(value: Any, attribute: str) -> str:
    """PropertyGroup의 비어 있는 UUID를 생성한 뒤 반환합니다."""
    token = str(getattr(value, attribute, "") or "")
    if not token:
        token = new_uuid()
        setattr(value, attribute, token)
    return token


def _active_layer(settings: Any):
    """현재 활성 Index의 Layer 또는 첫 Layer를 반환합니다."""
    if len(settings.layers) == 0:
        return None
    index = int(settings.active_layer_index)
    if not 0 <= index < len(settings.layers):
        index = 0
        settings.active_layer_index = 0
    return settings.layers[index]


def find_layer(settings: Any, layer_uuid: str):
    """UUID가 일치하는 Paint Layer를 반환합니다."""
    return next(
        (
            layer
            for layer in settings.layers
            if str(layer.layer_uuid) == str(layer_uuid)
        ),
        None,
    )


def find_adjustment(settings: Any, adjustment_uuid: str):
    """UUID가 일치하는 Layer와 Adjustment를 함께 반환합니다."""
    for layer in settings.layers:
        for adjustment in layer.adjustments:
            if str(adjustment.adjustment_uuid) == str(adjustment_uuid):
                return layer, adjustment
    return None, None


def find_mask(settings: Any, mask_uuid: str):
    """UUID가 일치하는 Layer·Adjustment 부모와 Mask를 함께 반환합니다."""
    for layer in settings.layers:
        if str(layer.mask.mask_uuid) == str(mask_uuid):
            return layer, None, layer.mask
        for adjustment in layer.adjustments:
            if str(adjustment.mask.mask_uuid) == str(mask_uuid):
                return layer, adjustment, adjustment.mask
    return None, None, None


def _sync_paint_image_selection(scene: Any, settings: Any) -> None:
    """Blender Paint Image와 Mask UUID를 같은 트리 선택 상태로 동기화합니다."""
    active_layer = _active_layer(settings)
    if (
        active_layer is not None
        and original_layer_is_read_only(settings, active_layer)
        and not bool(settings.paint_mask_mode)
    ):
        settings.active_mask_uuid = ""
        settings.active_paint_image = None
        return
    active_image = settings.active_paint_image
    if not bool(settings.paint_mask_mode) and _pointer(active_image):
        layer_matches = tuple(
            (index, layer)
            for index, layer in enumerate(settings.layers)
            if _pointer(layer.image) == _pointer(active_image)
        )
        if len(layer_matches) > 1:
            raise LayerShaderError("활성 Paint Image에 연결된 Layer가 중복됩니다.")
        if layer_matches:
            layer_index, _layer = layer_matches[0]
            settings.active_layer_index = layer_index
            settings.active_mask_uuid = ""
            return
    paint_images = (active_image,) if _pointer(active_image) else ()
    mask_matches = []
    for layer_index, layer in enumerate(settings.layers):
        masks = ((-1, layer.mask),) + tuple(
            (index, adjustment.mask)
            for index, adjustment in enumerate(layer.adjustments)
        )
        for adjustment_index, mask in masks:
            if (
                str(mask.mask_uuid or "")
                and _pointer(mask.image)
                and any(_pointer(mask.image) == _pointer(image) for image in paint_images)
            ):
                mask_matches.append((layer_index, adjustment_index, mask))
    if len(mask_matches) > 1:
        raise LayerShaderError("활성 Paint Image에 연결된 Mask가 중복됩니다.")
    if mask_matches:
        layer_index, adjustment_index, mask = mask_matches[0]
        settings.active_layer_index = layer_index
        settings.layers[layer_index].active_adjustment_index = adjustment_index
        settings.active_mask_uuid = str(mask.mask_uuid)
        settings.active_paint_image = mask.image
        settings.paint_mask_mode = True
        return
    active = _active_layer(settings)
    if active is not None and _pointer(active.image):
        settings.active_mask_uuid = ""
        settings.paint_mask_mode = False
        settings.active_paint_image = (
            None
            if original_layer_is_read_only(settings, active)
            else active.image
        )
        return
    active_mask_uuid = str(settings.active_mask_uuid or "")
    if active_mask_uuid:
        layer, adjustment, mask = find_mask(settings, active_mask_uuid)
        if mask is None:
            settings.active_mask_uuid = ""
            settings.paint_mask_mode = False
            return
        settings.active_layer_index = next(
            index
            for index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        layer.active_adjustment_index = (
            next(
                index
                for index, candidate in enumerate(layer.adjustments)
                if str(candidate.adjustment_uuid)
                == str(adjustment.adjustment_uuid)
            )
            if adjustment is not None
            else -1
        )
        settings.active_paint_image = mask.image
        settings.paint_mask_mode = True


def _layer_image_name(target: Any, layer: Any) -> str:
    """Target과 Layer 이름으로 관리 Paint Image 이름을 만듭니다."""
    return f"{target.name}_{layer.name}_Paint"


def _mask_image_name(target: Any, layer: Any, adjustment: Any = None) -> str:
    """Target과 부모 이름으로 관리 Mask Image 이름을 만듭니다."""
    suffix = (
        f"_{str(adjustment.adjustment_type).title()}"
        if adjustment is not None
        else ""
    )
    return f"{target.name}_{layer.name}{suffix}_Mask"


def _migrate_legacy_layer_mask(settings: Any, layer: Any) -> bool:
    """구형 MASK Adjustment를 동일 UUID·Image의 Layer Mask로 무손실 이전합니다."""
    legacy_indices = tuple(
        index
        for index, adjustment in enumerate(layer.adjustments)
        if str(adjustment.adjustment_type) == "MASK"
    )
    if not legacy_indices:
        return False
    if len(legacy_indices) != 1:
        raise LayerShaderError("구형 Layer Mask 항목이 중복되었습니다.")
    legacy_index = legacy_indices[0]
    legacy = layer.adjustments[legacy_index]
    legacy_uuid = _ensure_uuid(legacy, "adjustment_uuid")
    mask = layer.mask
    existing_uuid = str(mask.mask_uuid or "")
    if existing_uuid and existing_uuid != legacy_uuid:
        raise LayerShaderError("새 Layer Mask와 구형 Mask가 충돌합니다.")
    if (
        existing_uuid
        and _pointer(mask.image)
        and _pointer(legacy.image)
        and _pointer(mask.image) != _pointer(legacy.image)
    ):
        raise LayerShaderError("Layer Mask Image를 하나로 확정할 수 없습니다.")
    selected = int(layer.active_adjustment_index) == legacy_index
    legacy_image = legacy.image
    legacy_enabled = bool(legacy.enabled)
    if _pointer(legacy_image) and not _owned_image(
        legacy_image,
        {MASK_IMAGE_ROLE},
    ):
        raise LayerShaderError("구형 Layer Mask Image의 소유권이 유효하지 않습니다.")
    mask.mask_uuid = legacy_uuid
    if not _pointer(mask.image):
        mask.image = legacy_image
    mask["enabled"] = legacy_enabled
    if str(mask.mask_uuid) != legacy_uuid or _pointer(mask.image) != _pointer(legacy_image):
        raise LayerShaderError("구형 Layer Mask 이전 검증에 실패했습니다.")
    legacy.image = None
    layer.adjustments.remove(legacy_index)
    active_index = int(layer.active_adjustment_index)
    if selected:
        layer.active_adjustment_index = -1
        settings.active_mask_uuid = legacy_uuid
    elif active_index > legacy_index:
        layer.active_adjustment_index = active_index - 1
    return True


def _migrate_hue_saturation_adjustment(layer: Any) -> bool:
    """구버전 Hue와 Saturation 항목을 단일 Hue/Saturation 항목으로 통합합니다."""
    hue_indices = tuple(
        index
        for index, adjustment in enumerate(layer.adjustments)
        if str(adjustment.adjustment_type) == "HUE"
    )
    saturation_indices = tuple(
        index
        for index, adjustment in enumerate(layer.adjustments)
        if str(adjustment.adjustment_type) == "SATURATION"
    )
    if len(hue_indices) > 1 or len(saturation_indices) > 1:
        raise LayerShaderError("Hue/Saturation 보정 항목이 중복되었습니다.")
    if not hue_indices:
        return False
    hue_index = hue_indices[0]
    legacy_hue = layer.adjustments[hue_index]
    if not saturation_indices:
        legacy_hue.adjustment_type = "SATURATION"
        return True
    saturation_index = saturation_indices[0]
    saturation = layer.adjustments[saturation_index]
    active_index = int(layer.active_adjustment_index)
    active_uuid = (
        str(layer.adjustments[active_index].adjustment_uuid)
        if 0 <= active_index < len(layer.adjustments)
        else ""
    )
    hue_enabled = bool(legacy_hue.enabled)
    saturation_enabled = bool(saturation.enabled)
    preserve_disabled = not hue_enabled and not saturation_enabled
    saturation["hue"] = (
        float(legacy_hue.hue)
        if hue_enabled or preserve_disabled
        else 0.5
    )
    saturation["saturation"] = (
        float(saturation.saturation)
        if saturation_enabled or preserve_disabled
        else 1.0
    )
    saturation["enabled"] = hue_enabled or saturation_enabled
    selected_uuid = (
        str(saturation.adjustment_uuid)
        if active_uuid == str(legacy_hue.adjustment_uuid)
        else active_uuid
    )
    layer.adjustments.remove(hue_index)
    layer.active_adjustment_index = next(
        (
            index
            for index, adjustment in enumerate(layer.adjustments)
            if str(adjustment.adjustment_uuid) == selected_uuid
        ),
        -1,
    )
    return True


def _ensure_mask_image(
    target: Any,
    target_uuid: str,
    layer: Any,
    mask: Any,
    size: tuple[int, int],
    adjustment: Any = None,
    *,
    allow_legacy_promotion: bool = False,
    reference: Any = None,
) -> None:
    """Mask Image를 Stack과 같은 백엔드·타일·해상도 계약으로 보장합니다."""
    mask_uuid = str(mask.mask_uuid or "")
    if not mask_uuid:
        return
    adjustment_uuid = (
        str(adjustment.adjustment_uuid) if adjustment is not None else ""
    )
    if not _pointer(mask.image):
        mask.image = _new_image(
            target,
            _mask_image_name(target, layer, adjustment),
            size,
            role=MASK_IMAGE_ROLE,
            layer_uuid=str(layer.layer_uuid),
            adjustment_uuid=adjustment_uuid,
            mask_uuid=mask_uuid,
            color=(1.0, 1.0, 1.0, 1.0),
            reference=reference,
        )
    else:
        if not _owned_image(mask.image, {MASK_IMAGE_ROLE}):
            raise LayerShaderError("Mask Image의 관리 소유권이 유효하지 않습니다.")
        _validate_target_image_contract(target, mask.image, size, reference)
        expected = (
            str(target_uuid),
            str(layer.layer_uuid),
            str(adjustment_uuid),
            str(mask_uuid),
        )
        actual = (
            str(mask.image.get(TARGET_UUID_KEY, "") or ""),
            str(mask.image.get(LAYER_UUID_KEY, "") or ""),
            str(mask.image.get(ADJUSTMENT_UUID_KEY, "") or ""),
            str(mask.image.get(MASK_UUID_KEY, "") or ""),
        )
        if actual != expected and bool(allow_legacy_promotion) and not any(actual):
            _tag_image(
                mask.image,
                MASK_IMAGE_ROLE,
                target_uuid,
                str(layer.layer_uuid),
                adjustment_uuid,
                mask_uuid,
            )
        elif actual != expected:
            raise LayerShaderError("Mask Image의 논리 소유 UUID가 일치하지 않습니다.")


def _ensure_layer_data(
    scene: Any,
    target: Any,
    overlay: Any,
) -> tuple[Any, ...]:
    """모든 기존 계약을 선검증한 뒤 논리 Layer와 관리 Image를 연결합니다."""
    settings = _settings(scene)
    if settings is None:
        raise LayerShaderError("Projection Paint Scene 설정이 없습니다.")
    if len(settings.layers) == 0:
        layer = settings.layers.add()
        layer.name = "Layer 1"
        layer.layer_uuid = new_uuid()
        settings.active_layer_index = 0
    logical_images = tuple(
        layer.image for layer in settings.layers if _pointer(layer.image)
    )
    if len({_pointer(image) for image in logical_images}) != len(logical_images):
        raise LayerShaderError("여러 Layer가 같은 Paint Image를 참조합니다.")
    reference = _managed_layer_reference(settings) or overlay
    if not _owned_image(reference, {LAYER_IMAGE_ROLE}):
        raise LayerShaderError("관리 Paint Layer 해상도의 기준 Image가 없습니다.")
    size = _image_size(reference)
    _validate_target_image_contract(target, reference, size, reference)
    for layer in settings.layers:
        if _pointer(layer.image) and original_layer_is_read_only(settings, layer):
            _external_original_image(layer.image, require_files=True)
        elif _pointer(layer.image):
            _validate_target_image_contract(target, layer.image, size, reference)
        for mask in (
            layer.mask,
            *(adjustment.mask for adjustment in layer.adjustments),
        ):
            if _pointer(mask.image):
                _validate_target_image_contract(target, mask.image, size, reference)

    promoted_legacy_masks = set()
    for layer in settings.layers:
        if _migrate_legacy_layer_mask(settings, layer):
            promoted_legacy_masks.add(str(layer.mask.mask_uuid))
        _migrate_hue_saturation_adjustment(layer)
    mask_uuids = [
        mask_uuid
        for layer in settings.layers
        for mask_uuid in (
            str(layer.mask.mask_uuid or ""),
            *(str(item.mask.mask_uuid or "") for item in layer.adjustments),
        )
        if mask_uuid
    ]
    if len(mask_uuids) != len(set(mask_uuids)):
        raise LayerShaderError("Layer와 Adjustment Mask UUID가 중복되었습니다.")
    target_uuid = _target_uuid(target)
    for index, layer in enumerate(settings.layers):
        layer_uuid = _ensure_uuid(layer, "layer_uuid")
        if not _pointer(layer.image):
            if not logical_images and index == 0:
                layer.image = overlay
                _tag_image(
                    layer.image,
                    LAYER_IMAGE_ROLE,
                    target_uuid,
                    layer_uuid,
                )
            else:
                layer.image = _new_image(
                    target,
                    _layer_image_name(target, layer),
                    size,
                    role=LAYER_IMAGE_ROLE,
                    layer_uuid=layer_uuid,
                    color=(0.0, 0.0, 0.0, 0.0),
                    reference=reference,
                )
        else:
            if not original_layer_is_read_only(settings, layer):
                if not _owned_image(layer.image, {LAYER_IMAGE_ROLE}):
                    raise LayerShaderError("Layer Image의 관리 소유권이 유효하지 않습니다.")
                _validate_target_image_contract(target, layer.image, size, reference)
                actual_target = str(layer.image.get(TARGET_UUID_KEY, "") or "")
                actual_layer = str(layer.image.get(LAYER_UUID_KEY, "") or "")
                if actual_target != target_uuid or actual_layer != layer_uuid:
                    raise LayerShaderError("Layer Image의 논리 소유 UUID가 일치하지 않습니다.")
        _ensure_mask_image(
            target,
            target_uuid,
            layer,
            layer.mask,
            size,
            allow_legacy_promotion=(
                str(layer.mask.mask_uuid or "") in promoted_legacy_masks
            ),
            reference=reference,
        )
        for adjustment in layer.adjustments:
            _ensure_uuid(adjustment, "adjustment_uuid")
            if str(adjustment.adjustment_type) == "MASK":
                raise LayerShaderError("구형 Layer Mask 이전이 완료되지 않았습니다.")
            _ensure_mask_image(
                target,
                target_uuid,
                layer,
                adjustment.mask,
                size,
                adjustment,
                reference=reference,
            )
    return tuple(settings.layers)


def composite_session_is_active(settings: Any) -> bool:
    """저장된 Composite 세션이 편집 가능한 활성 상태인지 확인합니다."""
    return bool(
        settings is not None
        and str(getattr(settings, "composite_state", "NONE")) == "ACTIVE"
        and str(getattr(settings, "active_composite_session_uuid", "") or "")
    )


def composite_state_locks_layer_edits(settings: Any) -> bool:
    """Composite 처리 중이거나 손상된 상태에서 모든 Layer 데이터 변경을 차단해야 하는지 반환합니다."""
    return str(getattr(settings, "composite_state", "NONE")) in {
        "CREATING",
        "RECOMPOSITING",
        "RELEASING",
        "STALE",
    }


def composite_chain_layers(settings: Any) -> tuple[Any, ...]:
    """활성 Composite 세션의 기준 Layer 하나를 UI 순서로 반환합니다."""
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
    if not session_uuid:
        return ()
    return tuple(
        layer
        for layer in settings.layers
        if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
        == COMPOSITE_LAYER_ROLE
        and str(getattr(layer, "composite_session_uuid", "") or "") == session_uuid
    )


def composite_source_layers(settings: Any) -> tuple[Any, ...]:
    """활성 세션의 Source 또는 생성 전 전체 일반 Layer를 UI 순서로 반환합니다."""
    normal_layers = tuple(
        layer
        for layer in settings.layers
        if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) == NORMAL_LAYER_ROLE
    )
    state = str(getattr(settings, "composite_state", "NONE"))
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
    if (
        state not in {"ACTIVE", "RELEASING"}
        or not session_uuid
        or int(getattr(settings, "composite_source_schema_version", 0)) < 1
    ):
        return normal_layers
    return tuple(
        layer
        for layer in normal_layers
        if str(getattr(layer, "composite_source_session_uuid", "") or "")
        == session_uuid
    )


def composite_layer_is_editable(settings: Any, layer: Any) -> bool:
    """현재 Composite 상태에서 지정 Layer의 일반 편집 허용 여부를 반환합니다."""
    if composite_state_locks_layer_edits(settings):
        return False
    if not composite_session_is_active(settings):
        return True
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
    role = str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) if layer is not None else ""
    if role == NORMAL_LAYER_ROLE:
        if int(getattr(settings, "composite_source_schema_version", 0)) < 1:
            return False
        return str(getattr(layer, "composite_source_session_uuid", "") or "") != session_uuid
    return bool(
        layer is not None
        and role == COMPOSITE_LAYER_ROLE
        and str(getattr(layer, "composite_session_uuid", "") or "") == session_uuid
    )


def _graph_layers(settings: Any, layers: tuple[Any, ...]) -> tuple[Any, ...]:
    """현재 Composite 상태에 맞는 최종 Base Color Chain 대상 Layer를 선택합니다."""
    state = str(getattr(settings, "composite_state", "NONE"))
    if state == "STALE":
        raise LayerShaderError("손상된 Composite 상태에서는 Layer Shader Graph를 자동으로 재구성할 수 없습니다.")
    if state == "ACTIVE":
        session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
        composite_layers = tuple(
            layer
            for layer in layers
            if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
            == COMPOSITE_LAYER_ROLE
        )
        if any(
            str(getattr(layer, "composite_session_uuid", "") or "") != session_uuid
            for layer in composite_layers
        ):
            raise LayerShaderError("활성 Composite Layer의 세션 UUID가 일치하지 않습니다.")
        chain = composite_chain_layers(settings)
        if not chain or sum(
            str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) == COMPOSITE_LAYER_ROLE
            for layer in chain
        ) != 1:
            raise LayerShaderError("활성 Composite Layer 세션 계약이 유효하지 않습니다.")
        if int(getattr(settings, "composite_source_schema_version", 0)) < 1:
            return chain
        source_uuids = {
            str(layer.layer_uuid)
            for layer in composite_source_layers(settings)
        }
        if not source_uuids:
            raise LayerShaderError("활성 Composite Source 구간이 비어 있습니다.")
        graph_layers = tuple(
            layer
            for layer in layers
            if (
                str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
                == NORMAL_LAYER_ROLE
                and str(layer.layer_uuid) not in source_uuids
            )
            or (
                str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
                == COMPOSITE_LAYER_ROLE
                and str(getattr(layer, "composite_session_uuid", "") or "")
                == session_uuid
            )
        )
        if not graph_layers:
            raise LayerShaderError("활성 Composite 출력 Layer가 없습니다.")
        return graph_layers
    if state == "RELEASING":
        chain = tuple(
            layer
            for layer in layers
            if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
            == NORMAL_LAYER_ROLE
        )
        if not chain:
            raise LayerShaderError("복구할 원본 Paint Layer가 없습니다.")
        return chain
    return layers


def active_base_color_graph_layers(settings: Any) -> tuple[Any, ...]:
    """현재 Material Base Color 출력에 참여하는 Layer를 UI 순서로 읽어 반환합니다."""
    if settings is None:
        raise LayerShaderError("활성 Layer Graph를 읽을 Projection Paint 설정이 없습니다.")
    return _graph_layers(settings, tuple(settings.layers))


def _nodes_with_role(tree: Any, role: str) -> tuple[Any, ...]:
    """Node Tree에서 지정한 관리 역할의 Node를 반환합니다."""
    return tuple(node for node in tree.nodes if _owned_node(node, role))


def _node_for(
    tree: Any,
    role: str,
    layer_uuid: str,
    adjustment_uuid: str = "",
) -> Any | None:
    """역할과 UUID가 모두 일치하는 유일한 관리 Node를 반환합니다."""
    matches = tuple(
        node
        for node in tree.nodes
        if _owned_node(node, role)
        and str(node.get(LAYER_UUID_KEY, "")) == str(layer_uuid)
        and (
            not adjustment_uuid
            or str(node.get(ADJUSTMENT_UUID_KEY, ""))
            == str(adjustment_uuid)
        )
    )
    if len(matches) > 1:
        raise LayerShaderError(
            f"관리 Node 역할이 중복되었습니다: {role}, {layer_uuid}"
        )
    return matches[0] if matches else None


def _node_for_mask(
    tree: Any,
    role: str,
    layer_uuid: str,
    mask_uuid: str,
    adjustment_uuid: str = "",
) -> Any | None:
    """역할과 Mask UUID가 일치하는 유일한 관리 Node를 반환합니다."""
    matches = tuple(
        node
        for node in tree.nodes
        if _owned_node(node, role)
        and str(node.get(LAYER_UUID_KEY, "")) == str(layer_uuid)
        and (
            str(node.get(MASK_UUID_KEY, "")) == str(mask_uuid)
            or (
                not str(node.get(MASK_UUID_KEY, ""))
                and str(node.get(ADJUSTMENT_UUID_KEY, "")) == str(mask_uuid)
            )
        )
    )
    if len(matches) > 1:
        raise LayerShaderError(f"관리 Mask Node 역할이 중복되었습니다: {role}")
    if matches:
        _tag_node(
            matches[0],
            role,
            layer_uuid,
            adjustment_uuid,
            mask_uuid,
        )
        return matches[0]
    return None


def _new_node(
    tree: Any,
    type_name: str,
    role: str,
    layer_uuid: str,
    adjustment_uuid: str = "",
    mask_uuid: str = "",
) -> Any:
    """지정한 타입의 관리 Node를 생성하고 소유 UUID 역할을 기록합니다."""
    node = tree.nodes.new(type_name)
    _tag_node(node, role, layer_uuid, adjustment_uuid, mask_uuid)
    return node


def _legacy_nodes(tree: Any) -> tuple[Any | None, Any | None]:
    """Layer UUID가 없는 기존 단일 Overlay Image와 Mix Node를 찾습니다."""
    images = tuple(
        node
        for node in _nodes_with_role(tree, LAYER_IMAGE_NODE_ROLE)
        if not str(node.get(LAYER_UUID_KEY, ""))
    )
    mixes = tuple(
        node
        for node in _nodes_with_role(tree, LAYER_MIX_NODE_ROLE)
        if not str(node.get(LAYER_UUID_KEY, ""))
    )
    if len(images) > 1 or len(mixes) > 1:
        raise LayerShaderError("기존 단일 Overlay Node를 하나로 확정할 수 없습니다.")
    return (
        images[0] if images else None,
        mixes[0] if mixes else None,
    )


def _ensure_frame(tree: Any, layer: Any) -> Any:
    """Layer UUID에 대응하는 관리 Node Frame을 준비합니다."""
    frame = _node_for(
        tree,
        LAYER_FRAME_ROLE,
        str(layer.layer_uuid),
    )
    if frame is None:
        frame = _new_node(
            tree,
            "NodeFrame",
            LAYER_FRAME_ROLE,
            str(layer.layer_uuid),
        )
    frame.name = f"PP Layer {layer.layer_uuid}"
    frame.label = str(layer.name)
    frame.label_size = 24
    frame.shrink = True
    return frame


def _ensure_layer_nodes(
    tree: Any,
    layer: Any,
    *,
    legacy_image: Any = None,
    legacy_mix: Any = None,
) -> tuple[Any, Any, Any]:
    """Layer의 Frame, Image Texture와 Normal Mix Node를 준비합니다."""
    layer_uuid = str(layer.layer_uuid)
    frame = _ensure_frame(tree, layer)
    image_node = _node_for(tree, LAYER_IMAGE_NODE_ROLE, layer_uuid)
    if image_node is None:
        image_node = legacy_image or _new_node(
            tree,
            "ShaderNodeTexImage",
            LAYER_IMAGE_NODE_ROLE,
            layer_uuid,
        )
        _tag_node(image_node, LAYER_IMAGE_NODE_ROLE, layer_uuid)
    image_node.name = f"PP Layer Image {layer_uuid}"
    image_node.label = str(layer.name)
    image_node.image = layer.image
    image_node.width = 220.0
    mix_node = _node_for(tree, LAYER_MIX_NODE_ROLE, layer_uuid)
    if mix_node is None:
        mix_node = legacy_mix or _new_node(
            tree,
            "ShaderNodeMixRGB",
            LAYER_MIX_NODE_ROLE,
            layer_uuid,
        )
        _tag_node(mix_node, LAYER_MIX_NODE_ROLE, layer_uuid)
    mix_node.name = f"PP Layer Mix {layer_uuid}"
    mix_node.label = f"{layer.name} Normal"
    mix_node.blend_type = "MIX"
    mix_node.width = 180.0
    image_node.parent = frame
    mix_node.parent = frame
    return frame, image_node, mix_node


def _ensure_composite_unpremultiply_nodes(
    tree: Any,
    layer: Any,
    frame: Any,
    image_node: Any,
) -> Any | None:
    """Combined Bake Image의 premultiplied RGB를 일반 Layer Mix용 직선 색으로 되돌립니다."""
    image = getattr(layer, "image", None)
    is_combined_bake = bool(
        str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) == COMPOSITE_LAYER_ROLE
        or bool(getattr(image, "get", lambda *_args: False)(COMBINED_RGBA_IMAGE_KEY, False))
    )
    if not is_combined_bake:
        return None
    layer_uuid = str(layer.layer_uuid)
    separate = _node_for(
        tree,
        COMPOSITE_UNPREMULTIPLY_SEPARATE_NODE_ROLE,
        layer_uuid,
    ) or _new_node(
        tree,
        "ShaderNodeSeparateColor",
        COMPOSITE_UNPREMULTIPLY_SEPARATE_NODE_ROLE,
        layer_uuid,
    )
    alpha = _node_for(
        tree,
        COMPOSITE_UNPREMULTIPLY_ALPHA_NODE_ROLE,
        layer_uuid,
    ) or _new_node(
        tree,
        "ShaderNodeMath",
        COMPOSITE_UNPREMULTIPLY_ALPHA_NODE_ROLE,
        layer_uuid,
    )
    combine = _node_for(
        tree,
        COMPOSITE_UNPREMULTIPLY_COMBINE_NODE_ROLE,
        layer_uuid,
    ) or _new_node(
        tree,
        "ShaderNodeCombineColor",
        COMPOSITE_UNPREMULTIPLY_COMBINE_NODE_ROLE,
        layer_uuid,
    )
    divide_roles = (
        COMPOSITE_UNPREMULTIPLY_RED_DIVIDE_NODE_ROLE,
        COMPOSITE_UNPREMULTIPLY_GREEN_DIVIDE_NODE_ROLE,
        COMPOSITE_UNPREMULTIPLY_BLUE_DIVIDE_NODE_ROLE,
    )
    divide_nodes = tuple(
        _node_for(tree, role, layer_uuid)
        or _new_node(tree, "ShaderNodeMath", role, layer_uuid)
        for role in divide_roles
    )
    separate.name = f"PP Composite Unpremultiply Separate {layer_uuid}"
    separate.label = "Composite RGB 분리"
    separate.mode = "RGB"
    alpha.name = f"PP Composite Unpremultiply Alpha {layer_uuid}"
    alpha.label = "Composite Alpha 하한"
    alpha.operation = "MAXIMUM"
    alpha.inputs[0].default_value = 0.000001
    combine.name = f"PP Composite Unpremultiply Combine {layer_uuid}"
    combine.label = "Composite 직선 RGB"
    combine.mode = "RGB"
    _connect(tree, image_node.outputs["Color"], separate.inputs["Color"])
    _connect(tree, image_node.outputs["Alpha"], alpha.inputs[1])
    for source_socket, target_socket, divide in zip(
        (separate.outputs["Red"], separate.outputs["Green"], separate.outputs["Blue"]),
        (combine.inputs["Red"], combine.inputs["Green"], combine.inputs["Blue"]),
        divide_nodes,
    ):
        divide.name = f"PP Composite Unpremultiply {target_socket.name} {layer_uuid}"
        divide.label = f"Composite {target_socket.name} / Alpha"
        divide.operation = "DIVIDE"
        _connect(tree, source_socket, divide.inputs[0])
        _connect(tree, alpha.outputs[0], divide.inputs[1])
        _connect(tree, divide.outputs[0], target_socket)
    for node in (separate, alpha, combine, *divide_nodes):
        node.parent = frame
    return combine


def _ensure_adjustment_node(tree: Any, layer: Any, adjustment: Any) -> Any:
    """Brightness·Exposure·Hue/Saturation 대응 Shader Node를 준비합니다."""
    layer_uuid = str(layer.layer_uuid)
    adjustment_uuid = str(adjustment.adjustment_uuid)
    node = _node_for(
        tree,
        ADJUSTMENT_NODE_ROLE,
        layer_uuid,
        adjustment_uuid,
    )
    adjustment_type = str(adjustment.adjustment_type)
    expected = {
        "BRIGHTNESS": "BRIGHTCONTRAST",
        "EXPOSURE": "MIX_RGB",
        "HUE": "HUE_SAT",
        "SATURATION": "HUE_SAT",
    }.get(adjustment_type)
    if expected is None:
        raise LayerShaderError(f"지원하지 않는 색상 보정입니다: {adjustment_type}")
    if node is not None and node.type != expected:
        tree.nodes.remove(node)
        node = None
    if node is None:
        type_name = {
            "BRIGHTNESS": "ShaderNodeBrightContrast",
            "EXPOSURE": "ShaderNodeMixRGB",
            "HUE": "ShaderNodeHueSaturation",
            "SATURATION": "ShaderNodeHueSaturation",
        }[adjustment_type]
        node = _new_node(
            tree,
            type_name,
            ADJUSTMENT_NODE_ROLE,
            layer_uuid,
            adjustment_uuid,
        )
    display_name = (
        "Hue/Saturation"
        if adjustment_type in {"HUE", "SATURATION"}
        else adjustment_type.title()
    )
    node.name = f"PP {display_name} {adjustment_uuid}"
    node.label = display_name
    node.width = 180.0
    if adjustment_type == "EXPOSURE":
        node.blend_type = "MULTIPLY"
        node.inputs[0].default_value = 1.0
    sync_adjustment_node(node, adjustment)
    return node


def _ensure_layer_mask_nodes(
    tree: Any,
    layer: Any,
) -> tuple[Any, Any, Any]:
    """Layer Mask의 Image, 흑백 변환과 Alpha 곱셈 Node를 준비합니다."""
    layer_uuid = str(layer.layer_uuid)
    mask_uuid = str(layer.mask.mask_uuid)
    image = _node_for_mask(
        tree,
        MASK_IMAGE_NODE_ROLE,
        layer_uuid,
        mask_uuid,
    )
    if image is None:
        image = _new_node(
            tree,
            "ShaderNodeTexImage",
            MASK_IMAGE_NODE_ROLE,
            layer_uuid,
            mask_uuid=mask_uuid,
        )
    image.name = f"PP Layer Mask Image {mask_uuid}"
    image.label = "Mask"
    image.image = layer.mask.image
    image.width = 220.0
    bw = _node_for_mask(
        tree,
        MASK_BW_NODE_ROLE,
        layer_uuid,
        mask_uuid,
    )
    if bw is None:
        bw = _new_node(
            tree,
            "ShaderNodeRGBToBW",
            MASK_BW_NODE_ROLE,
            layer_uuid,
            mask_uuid=mask_uuid,
        )
    bw.name = f"PP Layer Mask BW {mask_uuid}"
    bw.label = "Mask Grayscale"
    bw.width = 150.0
    multiply = _node_for_mask(
        tree,
        MASK_MULTIPLY_NODE_ROLE,
        layer_uuid,
        mask_uuid,
    )
    if multiply is None:
        multiply = _new_node(
            tree,
            "ShaderNodeMath",
            MASK_MULTIPLY_NODE_ROLE,
            layer_uuid,
            mask_uuid=mask_uuid,
        )
    multiply.name = f"PP Layer Mask Alpha {mask_uuid}"
    multiply.label = "Layer Alpha × Mask"
    multiply.operation = "MULTIPLY"
    multiply.width = 170.0
    return image, bw, multiply


def _ensure_adjustment_apply_mix(tree: Any, layer: Any, adjustment: Any) -> Any:
    """Adjustment마다 원본과 보정 결과를 선택하는 고정 Apply Mix를 준비합니다."""
    layer_uuid = str(layer.layer_uuid)
    adjustment_uuid = str(adjustment.adjustment_uuid)
    node = _node_for(
        tree,
        ADJUSTMENT_APPLY_MIX_NODE_ROLE,
        layer_uuid,
        adjustment_uuid,
    )
    if node is None:
        node = _new_node(
            tree,
            "ShaderNodeMixRGB",
            ADJUSTMENT_APPLY_MIX_NODE_ROLE,
            layer_uuid,
            adjustment_uuid,
        )
    node.name = f"PP Adjustment Apply {adjustment_uuid}"
    node.label = "Adjustment Apply"
    node.blend_type = "MIX"
    node.width = 180.0
    return node


def _ensure_adjustment_mask_nodes(
    tree: Any,
    layer: Any,
    adjustment: Any,
) -> tuple[Any, Any]:
    """Adjustment Mask의 Image와 흑백 Factor Node를 준비합니다."""
    layer_uuid = str(layer.layer_uuid)
    adjustment_uuid = str(adjustment.adjustment_uuid)
    mask_uuid = str(adjustment.mask.mask_uuid)
    image = _node_for_mask(
        tree,
        ADJUSTMENT_MASK_IMAGE_NODE_ROLE,
        layer_uuid,
        mask_uuid,
        adjustment_uuid,
    )
    if image is None:
        image = _new_node(
            tree,
            "ShaderNodeTexImage",
            ADJUSTMENT_MASK_IMAGE_NODE_ROLE,
            layer_uuid,
            adjustment_uuid,
            mask_uuid,
        )
    image.name = f"PP Adjustment Mask Image {mask_uuid}"
    image.label = "Adjustment Mask"
    image.image = adjustment.mask.image
    image.width = 220.0
    bw = _node_for_mask(
        tree,
        ADJUSTMENT_MASK_BW_NODE_ROLE,
        layer_uuid,
        mask_uuid,
        adjustment_uuid,
    )
    if bw is None:
        bw = _new_node(
            tree,
            "ShaderNodeRGBToBW",
            ADJUSTMENT_MASK_BW_NODE_ROLE,
            layer_uuid,
            adjustment_uuid,
            mask_uuid,
        )
    bw.name = f"PP Adjustment Mask BW {mask_uuid}"
    bw.label = "Adjustment Mask Grayscale"
    bw.width = 160.0
    return image, bw


def sync_adjustment_node(node: Any, adjustment: Any) -> None:
    """UI 보정값을 대응 Shader Node에 전달합니다."""
    adjustment_type = str(adjustment.adjustment_type)
    node.mute = False
    if adjustment_type == "BRIGHTNESS":
        node.inputs["Brightness"].default_value = float(adjustment.brightness)
        node.inputs["Contrast"].default_value = 0.0
    elif adjustment_type == "EXPOSURE":
        gain = float(pow(2.0, float(adjustment.exposure)))
        node.inputs[2].default_value = (gain, gain, gain, 1.0)
    elif adjustment_type in {"HUE", "SATURATION"}:
        node.inputs["Hue"].default_value = float(adjustment.hue)
        node.inputs["Saturation"].default_value = float(adjustment.saturation)
        node.inputs["Value"].default_value = 1.0
        node.inputs["Factor"].default_value = 1.0


def _clear_input(tree: Any, socket: Any) -> None:
    """입력 Socket에 연결된 모든 Link를 제거합니다."""
    for link in tuple(socket.links):
        tree.links.remove(link)


def _connect(tree: Any, output: Any, input_socket: Any) -> None:
    """입력 Socket의 기존 Link를 제거한 뒤 새 Link 하나를 연결합니다."""
    _clear_input(tree, input_socket)
    tree.links.new(output, input_socket)


def _principled(material: Any) -> Any:
    """관리 Material의 활성 Principled BSDF를 반환합니다."""
    outputs = tuple(
        node
        for node in material.node_tree.nodes
        if node.type == "OUTPUT_MATERIAL" and bool(node.is_active_output)
    )
    if len(outputs) != 1:
        raise LayerShaderError("활성 Material Output이 정확히 하나여야 합니다.")
    surface = outputs[0].inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        raise LayerShaderError("Material Output Surface 연결이 유효하지 않습니다.")
    principled = surface.links[0].from_node
    if principled.type != "BSDF_PRINCIPLED":
        raise LayerShaderError("활성 Material은 Principled BSDF를 사용해야 합니다.")
    return principled


def _capture_base_input(
    tree: Any,
    mix_nodes: tuple[Any, ...],
) -> tuple[Any | None, tuple[float, float, float, float]]:
    """기존 Layer Chain 아래의 Base Color Link 또는 기본값을 보존합니다."""
    mix_pointers = {_pointer(node) for node in mix_nodes}
    external = []
    candidates = []
    for mix in mix_nodes:
        socket = mix.inputs[1]
        if len(socket.links) == 0:
            candidates.append(tuple(float(value) for value in socket.default_value))
            continue
        link = socket.links[0]
        if _pointer(link.from_node) not in mix_pointers:
            external.append(link.from_socket)
            candidates.append(tuple(float(value) for value in socket.default_value))
    if len(external) > 1:
        raise LayerShaderError("Layer Chain의 Base Color 입력이 둘 이상입니다.")
    default = candidates[0] if candidates else (0.8, 0.8, 0.8, 1.0)
    return (external[0] if external else None), default


def _original_gate(tree: Any) -> Any | None:
    """소유 태그로 식별되는 유일한 Original Base Color Gate를 반환합니다."""
    matches = _nodes_with_role(tree, ORIGINAL_GATE_NODE_ROLE)
    if len(matches) > 1:
        raise LayerShaderError("Original Base Color Gate가 둘 이상 존재합니다.")
    return matches[0] if matches else None


def _original_gate_raw_input(
    gate: Any,
) -> tuple[Any | None, tuple[float, float, float, float]]:
    """Original Gate가 보존한 원본 Socket 또는 기본 Base Color 값을 반환합니다."""
    if gate.type != "MIX_RGB" or len(gate.inputs) < 3:
        raise LayerShaderError("Original Base Color Gate 유형이 유효하지 않습니다.")
    raw_input = gate.inputs[2]
    if len(raw_input.links) > 1:
        raise LayerShaderError("Original Base Color Gate 원본 입력이 둘 이상 연결되었습니다.")
    raw_source = raw_input.links[0].from_socket if raw_input.links else None
    return raw_source, tuple(float(value) for value in raw_input.default_value)


def _original_base_input(
    tree: Any,
    mix_nodes: tuple[Any, ...],
) -> tuple[Any | None, tuple[float, float, float, float]]:
    """Original Gate가 있으면 보존 원본을, 없으면 기존 Base 입력을 반환합니다."""
    gate = _original_gate(tree)
    if gate is not None:
        return _original_gate_raw_input(gate)
    return _capture_base_input(tree, mix_nodes)


def original_layer(settings: Any) -> Any | None:
    """현재 Stack에 남아 있는 실제 Original Layer를 하나만 반환합니다."""
    matches = tuple(
        layer
        for layer in settings.layers
        if bool(getattr(layer, "is_original_layer", False))
    )
    if len(matches) > 1:
        raise LayerShaderError("Original Layer가 둘 이상 존재합니다.")
    return matches[0] if matches else None


def _prepare_original_reference_migration(settings: Any) -> dict[str, Any] | None:
    """기존 복제 Original을 외부 Source Image 직접 참조로 원자 이관 준비합니다."""
    current = original_layer(settings)
    schema = int(getattr(settings, "original_layer_schema_version", 0))
    if current is None or schema < 1:
        return None
    source = getattr(settings, "original_source_image", None)
    external = _external_original_image(source, require_files=True)
    if external is None:
        if schema < ORIGINAL_LAYER_SCHEMA_VERSION:
            settings.original_layer_schema_version = ORIGINAL_LAYER_SCHEMA_VERSION
            return {
                "layer": current,
                "previous_image": current.image,
                "previous_schema": schema,
                "protected_image": None,
            }
        return None
    if (
        schema >= ORIGINAL_LAYER_SCHEMA_VERSION
        and _pointer(current.image) == _pointer(external)
    ):
        return None
    previous = current.image
    protected = (
        previous
        if _owned_image(previous, {LAYER_IMAGE_ROLE})
        and _pointer(previous) != _pointer(external)
        else None
    )
    if protected is not None:
        protected[ORIGINAL_REFERENCE_PENDING_DELETE_KEY] = True
    current.image = external
    settings.original_layer_schema_version = ORIGINAL_LAYER_SCHEMA_VERSION
    return {
        "layer": current,
        "previous_image": previous,
        "previous_schema": schema,
        "protected_image": protected,
    }


def _finish_original_reference_migration(snapshot: dict[str, Any] | None) -> None:
    """직접 참조 이관이 끝난 뒤 과거 관리 복제본만 안전하게 정리합니다."""
    if snapshot is None:
        return
    protected = snapshot.get("protected_image")
    if _pointer(protected) and ORIGINAL_REFERENCE_PENDING_DELETE_KEY in protected:
        del protected[ORIGINAL_REFERENCE_PENDING_DELETE_KEY]
    if _pointer(protected) and int(getattr(protected, "users", 0)) == 0:
        _remove_image(protected)


def _rollback_original_reference_migration(
    settings: Any,
    snapshot: dict[str, Any] | None,
) -> None:
    """직접 참조 이관 실패 시 기존 Image Pointer와 Schema를 복구합니다."""
    if snapshot is None:
        return
    layer_uuid = str(getattr(snapshot.get("layer"), "layer_uuid", "") or "")
    current = find_layer(settings, layer_uuid)
    if current is not None:
        current.image = snapshot.get("previous_image")
    material = getattr(settings, "paint_material", None)
    if material is not None and material.node_tree is not None and layer_uuid:
        node = _node_for(material.node_tree, LAYER_IMAGE_NODE_ROLE, layer_uuid)
        if node is not None:
            node.image = snapshot.get("previous_image")
    settings.original_layer_schema_version = int(snapshot.get("previous_schema", 0))
    protected = snapshot.get("protected_image")
    if _pointer(protected) and ORIGINAL_REFERENCE_PENDING_DELETE_KEY in protected:
        del protected[ORIGINAL_REFERENCE_PENDING_DELETE_KEY]


def _original_source_from_input(
    source: Any | None,
    default: tuple[float, float, float, float],
) -> tuple[Any | None, tuple[float, float, float, float], str, Any | None]:
    """구형 Base Color 입력을 Image 또는 고정 RGBA Original 데이터로 정규화합니다."""
    color = tuple(float(value) for value in default)
    if source is None:
        return None, color, "Original Layer · Base Color", None
    node = getattr(source, "node", None)
    if getattr(node, "type", "") == "TEX_IMAGE" and _pointer(
        getattr(node, "image", None)
    ):
        image = node.image
        has_external_path = bool(
            str(getattr(image, "filepath", "") or getattr(image, "filepath_raw", ""))
        )
        has_packed_data = bool(
            getattr(image, "packed_file", None) is not None
            or len(tuple(getattr(image, "packed_files", ()))) > 0
        )
        if str(getattr(image, "source", "")) == "GENERATED":
            return image, color, "Original Layer · Texture", source
        if has_external_path or has_packed_data:
            return image, color, "Original Layer · Texture", None
        return None, color, "Original Layer · Texture", source
    if getattr(node, "type", "") == "RGB":
        value = getattr(source, "default_value", None)
        try:
            color = tuple(float(component) for component in value)
        except (TypeError, ValueError):
            raise LayerShaderError("Original RGB Socket 기본값이 유효하지 않습니다.")
        if len(color) != 4:
            raise LayerShaderError("Original RGB Socket 기본값이 RGBA가 아닙니다.")
        return None, color, "Original Layer · Base Color", None
    return None, color, "Original Layer · Material", source


def _remove_original_gates(tree: Any) -> None:
    """실제 Original Layer 전환이 확정된 뒤 구형 소유 Gate만 제거합니다."""
    for gate in _nodes_with_role(tree, ORIGINAL_GATE_NODE_ROLE):
        tree.nodes.remove(gate)


def migrate_original_layer(
    scene: Any,
    target: Any,
    material: Any,
    reference: Any,
) -> tuple[
    Any | None,
    Any | None,
    tuple[float, float, float, float] | None,
    Any | None,
]:
    """구형 Original Gate를 실제 하단 Normal Layer로 한 번만 원자적으로 이전합니다."""
    settings = _settings(scene)
    if settings is None:
        raise LayerShaderError("Projection Paint Scene 설정이 없습니다.")
    if int(getattr(settings, "original_layer_schema_version", 0)) >= ORIGINAL_LAYER_SCHEMA_VERSION:
        return original_layer(settings), None, None, None
    if original_layer(settings) is not None:
        raise LayerShaderError("완료되지 않은 Original Layer 이전이 있어 자동 재시도를 중지했습니다.")
    state = str(getattr(settings, "composite_state", "NONE"))
    if state == "STALE":
        raise LayerShaderError("손상된 Composite 상태에서는 Original Layer를 자동 이전하지 않습니다.")
    if state in {"CREATING", "RECOMPOSITING", "RELEASING"}:
        raise LayerShaderError("Composite 처리 중에는 Original Layer를 자동 이전하지 않습니다.")
    tree = material.node_tree
    mixes = _nodes_with_role(tree, LAYER_MIX_NODE_ROLE)
    source, default = _original_base_input(tree, mixes)
    source_image, color, label, source_socket = _original_source_from_input(
        source,
        default,
    )
    layer = settings.layers.add()
    layer.name = label
    layer.layer_uuid = new_uuid()
    layer.layer_role = NORMAL_LAYER_ROLE
    layer.is_original_layer = True
    layer.visible = bool(settings.original_layer_visible)
    layer.composite_saved_visible = bool(settings.original_layer_visible)
    image = None
    try:
        if source_socket is not None:
            image = _create_original_image(
                target,
                str(layer.layer_uuid),
                _layer_image_name(target, layer),
                _image_size(reference),
                None,
                color,
                reference,
                scene=scene,
                material=material,
                source_socket=source_socket,
            )
        else:
            image = _create_original_image(
                target,
                str(layer.layer_uuid),
                _layer_image_name(target, layer),
                _image_size(reference),
                source_image,
                color,
                reference,
            )
        layer.image = image
        if _owned_image(image, {LAYER_IMAGE_ROLE}):
            image[ORIGINAL_LAYER_IMAGE_KEY] = True
        return (
            layer,
            source_image,
            color,
            source_socket,
        )
    except Exception:
        index = len(settings.layers) - 1
        if 0 <= index < len(settings.layers) and _pointer(settings.layers[index]) == _pointer(layer):
            settings.layers.remove(index)
        if _owned_image(image, {LAYER_IMAGE_ROLE}) and int(image.users) == 0:
            _remove_image(image)
        raise


def restore_original_layer(context: Any) -> Any:
    """삭제된 Original Layer를 현재 Stack Image 계약으로 한 번만 복원합니다."""
    scene = context.scene
    settings = _settings(scene)
    if settings is None:
        raise LayerShaderError("Projection Paint Scene 설정이 없습니다.")
    if str(getattr(settings, "composite_state", "NONE")) != "NONE":
        raise LayerShaderError("Composite 세션 중에는 Original Layer를 복원할 수 없습니다.")
    if original_layer(settings) is not None:
        raise LayerShaderError("복원할 Original Layer가 이미 있습니다.")
    if int(getattr(settings, "original_layer_schema_version", 0)) < ORIGINAL_LAYER_SCHEMA_VERSION:
        raise LayerShaderError("구형 Original Layer 이전이 완료되지 않았습니다.")
    target = getattr(settings, "paint_target", None)
    material = getattr(settings, "paint_material", None)
    reference = next(
        (layer.image for layer in settings.layers if _pointer(layer.image)),
        None,
    )
    if target is None or material is None or material.node_tree is None or reference is None:
        raise LayerShaderError("Original Layer 복원에 필요한 현재 Layer Stack이 없습니다.")
    source_image = getattr(settings, "original_source_image", None)
    if source_image is not None and _pointer(source_image) == 0:
        raise LayerShaderError("저장된 Original Source Image가 더 이상 유효하지 않습니다.")
    source_socket = _original_source_socket(material, settings)
    color = tuple(float(value) for value in settings.original_source_color)
    name = (
        "Original Layer · Texture"
        if source_image is not None
        else (
            "Original Layer · Material"
            if source_socket is not None
            else "Original Layer · Base Color"
        )
    )
    layer = settings.layers.add()
    layer.name = name
    layer.layer_uuid = new_uuid()
    layer.layer_role = NORMAL_LAYER_ROLE
    layer.is_original_layer = True
    layer.visible = True
    layer.composite_saved_visible = True
    image = None
    try:
        image = _create_original_image(
            target,
            str(layer.layer_uuid),
            _layer_image_name(target, layer),
            _image_size(reference),
            source_image,
            color,
            reference,
            scene=scene,
            material=material,
            source_socket=source_socket,
        )
        layer.image = image
        if _owned_image(image, {LAYER_IMAGE_ROLE}):
            image[ORIGINAL_LAYER_IMAGE_KEY] = True
        reconcile_layer_graph(scene)
        settings.original_layer_name = str(layer.name)
        return layer
    except Exception:
        if _pointer(image):
            layer.image = None
        index = next(
            (
                position
                for position, candidate in enumerate(settings.layers)
                if str(candidate.layer_uuid) == str(layer.layer_uuid)
            ),
            -1,
        )
        if index >= 0:
            settings.layers.remove(index)
        if _owned_image(image, {LAYER_IMAGE_ROLE}) and int(image.users) == 0:
            _remove_image(image)
        try:
            reconcile_layer_graph(scene)
        except Exception as rollback_error:
            raise LayerShaderError(
                f"Original Layer 복원 실패 뒤 Layer Stack 복구에 실패했습니다: {rollback_error}"
            )
        raise


def _rollback_uncommitted_original_migration(
    scene: Any,
    material: Any,
    layer: Any,
    legacy_source: Any | None,
    legacy_default: tuple[float, float, float, float],
    settings_snapshot: dict[str, Any] | None = None,
    created_source_tag_node: Any | None = None,
) -> None:
    """새 체인 검증 실패 뒤 Gate와 기존 Layer Chain만 남도록 Original 이전을 되돌립니다."""
    settings = _settings(scene)
    if settings is None or int(settings.original_layer_schema_version) >= ORIGINAL_LAYER_SCHEMA_VERSION:
        return
    if _pointer(created_source_tag_node) and (
        ORIGINAL_SOURCE_NODE_UUID_KEY in created_source_tag_node
    ):
        del created_source_tag_node[ORIGINAL_SOURCE_NODE_UUID_KEY]
    layer_uuid = str(getattr(layer, "layer_uuid", "") or "")
    image = getattr(layer, "image", None)
    index = next(
        (
            position
            for position, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == layer_uuid
        ),
        -1,
    )
    if index < 0:
        return
    tree = material.node_tree
    for node in tuple(
        node
        for node in tree.nodes
        if _owned_node(node) and str(node.get(LAYER_UUID_KEY, "") or "") == layer_uuid
    ):
        tree.nodes.remove(node)
    settings.layers.remove(index)
    if _owned_image(image, {LAYER_IMAGE_ROLE}) and int(image.users) == 0:
        _remove_image(image)
    if settings_snapshot is not None:
        for key, value in settings_snapshot.items():
            setattr(settings, key, value)
    layers = tuple(settings.layers)
    graph_layers = _graph_layers(settings, layers)
    nodes_by_layer = {}
    for current in graph_layers:
        mix = _node_for(tree, LAYER_MIX_NODE_ROLE, str(current.layer_uuid))
        if mix is None:
            raise LayerShaderError("Original Layer 이전 롤백에 필요한 기존 Mix Node가 없습니다.")
        nodes_by_layer[str(current.layer_uuid)] = (None, None, mix)
    gate = _original_gate(tree)
    state = str(getattr(settings, "composite_state", "NONE"))
    if state != "ACTIVE" and gate is None and legacy_source is None:
        base_default = legacy_default
    else:
        base_default = (0.0, 0.0, 0.0, 1.0)
    _rebuild_layer_chain(
        material,
        graph_layers,
        nodes_by_layer,
        gate.outputs["Color"] if gate is not None and state != "ACTIVE" else (
            legacy_source if state != "ACTIVE" else None
        ),
        base_default,
    )
    _apply_preview(scene, material)
    _NODE_CACHE.clear()


def _original_migration_settings_snapshot(settings: Any) -> dict[str, Any]:
    """Original 이관 뒤 실패 시 되돌릴 Scene 설정값을 보관합니다."""
    return {
        "active_layer_index": int(settings.active_layer_index),
        "active_mask_uuid": str(settings.active_mask_uuid or ""),
        "paint_mask_mode": bool(settings.paint_mask_mode),
        "layer_preview_uuid": str(settings.layer_preview_uuid or ""),
        "mask_preview_adjustment_uuid": str(
            settings.mask_preview_adjustment_uuid or ""
        ),
        "overlay_image": settings.overlay_image,
        "active_paint_image": settings.active_paint_image,
        "original_layer_name": str(settings.original_layer_name or ""),
        "original_layer_preview": bool(settings.original_layer_preview),
        "original_source_image": settings.original_source_image,
        "original_source_color": tuple(settings.original_source_color),
        "original_source_node_uuid": str(
            settings.original_source_node_uuid or ""
        ),
        "original_source_socket_identifier": str(
            settings.original_source_socket_identifier or ""
        ),
    }


def _new_original_source_tag_node(source_socket: Any) -> Any | None:
    """이관 실패 시에만 제거해야 할 새 Source UUID 태그의 Node를 찾습니다."""
    node = getattr(source_socket, "node", None)
    if _pointer(node) == 0 or ORIGINAL_SOURCE_NODE_UUID_KEY in node:
        return None
    return node


def _link_layer_color(
    tree: Any,
    layer: Any,
    image_node: Any,
    mix_node: Any,
    adjustment_nodes: dict[str, Any],
    apply_mix_nodes: dict[str, Any],
    adjustment_mask_nodes: dict[str, tuple[Any, Any]],
    image_color: Any | None = None,
) -> None:
    """Layer 색상을 Adjustment와 고정 Apply Mix 체인으로 연결합니다."""
    current = image_color if image_color is not None else image_node.outputs["Color"]
    for adjustment in layer.adjustments:
        adjustment_uuid = str(adjustment.adjustment_uuid)
        node = adjustment_nodes[adjustment_uuid]
        apply_mix = apply_mix_nodes[adjustment_uuid]
        original = current
        color_input = (
            node.inputs["Color"]
            if str(adjustment.adjustment_type) != "EXPOSURE"
            else node.inputs[1]
        )
        _connect(tree, original, color_input)
        _connect(tree, original, apply_mix.inputs[1])
        _connect(tree, node.outputs["Color"], apply_mix.inputs[2])
        _sync_adjustment_apply_mix(
            tree,
            adjustment,
            apply_mix,
            adjustment_mask_nodes.get(str(adjustment.mask.mask_uuid)),
        )
        current = apply_mix.outputs["Color"]
    _connect(tree, current, mix_node.inputs[2])


def _sync_adjustment_apply_mix(
    tree: Any,
    adjustment: Any,
    apply_mix: Any,
    mask_nodes: tuple[Any, Any] | None,
) -> None:
    """Adjustment 활성 상태와 자체 Mask를 Apply Mix Factor에 반영합니다."""
    factor = apply_mix.inputs[0]
    _clear_input(tree, factor)
    if not bool(adjustment.enabled):
        factor.default_value = 0.0
        return
    mask = adjustment.mask
    if not str(mask.mask_uuid) or not bool(mask.enabled) or mask_nodes is None:
        factor.default_value = 1.0
        return
    image, bw = mask_nodes
    _connect(tree, image.outputs["Color"], bw.inputs["Color"])
    _connect(tree, bw.outputs["Val"], factor)


def _link_layer_factor(
    tree: Any,
    layer: Any,
    image_node: Any,
    mix_node: Any,
    mask_nodes: dict[str, tuple[Any, Any, Any]],
) -> None:
    """Layer Alpha와 활성 Mask를 Normal Mix Factor에 연결합니다."""
    if not bool(layer.visible):
        _clear_input(tree, mix_node.inputs[0])
        mix_node.inputs[0].default_value = 0.0
        return
    mask = layer.mask
    mask_uuid = str(mask.mask_uuid or "")
    if not mask_uuid:
        _connect(tree, image_node.outputs["Alpha"], mix_node.inputs[0])
        return
    image, bw, multiply = mask_nodes[mask_uuid]
    _connect(tree, image.outputs["Color"], bw.inputs["Color"])
    if not bool(mask.enabled):
        _connect(tree, image_node.outputs["Alpha"], mix_node.inputs[0])
        return
    _connect(tree, image_node.outputs["Alpha"], multiply.inputs[0])
    _connect(tree, bw.outputs["Val"], multiply.inputs[1])
    _connect(tree, multiply.outputs["Value"], mix_node.inputs[0])


def _valid_uuid_sets(settings: Any) -> tuple[set[str], set[str], set[str]]:
    """현재 Layer·Adjustment·Mask UUID 집합을 반환합니다."""
    layers = {str(layer.layer_uuid) for layer in settings.layers}
    adjustments = {
        str(adjustment.adjustment_uuid)
        for layer in settings.layers
        for adjustment in layer.adjustments
    }
    masks = {
        mask_uuid
        for layer in settings.layers
        for mask_uuid in (
            str(layer.mask.mask_uuid or ""),
            *(str(item.mask.mask_uuid or "") for item in layer.adjustments),
        )
        if mask_uuid
    }
    return layers, adjustments, masks


def _stale_nodes(tree: Any, settings: Any) -> tuple[Any, ...]:
    """PropertyGroup에 없는 UUID의 관리 Node를 변경 없이 반환합니다."""
    layer_uuids, adjustment_uuids, mask_uuids = _valid_uuid_sets(settings)
    combined_bake_layer_uuids = {
        str(layer.layer_uuid)
        for layer in settings.layers
        if (
            str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
            == COMPOSITE_LAYER_ROLE
            or bool(
                getattr(getattr(layer, "image", None), "get", lambda *_args: False)(
                    COMBINED_RGBA_IMAGE_KEY,
                    False,
                )
            )
        )
    }
    stale = []
    for node in tree.nodes:
        role = str(node.get(RECORD_KIND_KEY, ""))
        if not _owned_node(node) or role not in _MANAGED_NODE_ROLES:
            continue
        layer_uuid = str(node.get(LAYER_UUID_KEY, ""))
        adjustment_uuid = str(node.get(ADJUSTMENT_UUID_KEY, ""))
        mask_uuid = str(node.get(MASK_UUID_KEY, ""))
        if layer_uuid and layer_uuid not in layer_uuids:
            stale.append(node)
        elif (
            role.startswith("COMPOSITE_UNPREMULTIPLY_")
            and layer_uuid not in combined_bake_layer_uuids
        ):
            stale.append(node)
        elif adjustment_uuid and adjustment_uuid not in adjustment_uuids:
            stale.append(node)
        elif mask_uuid and mask_uuid not in mask_uuids:
            stale.append(node)
    return tuple(stale)


def _logical_image_pointers(settings: Any) -> set[int]:
    """현재 논리 Layer와 Mask가 참조하는 Image Pointer 집합을 반환합니다."""
    images = []
    for layer in settings.layers:
        images.append(layer.image)
        images.append(layer.mask.image)
        images.extend(adjustment.mask.image for adjustment in layer.adjustments)
    return {_pointer(image) for image in images if _pointer(image)}


def _reconcile_stale_resources(tree: Any, settings: Any, target: Any) -> None:
    """고아 관리 Node와 안전하게 미참조된 관리 Image를 한 번 정리합니다."""
    stale_nodes = _stale_nodes(tree, settings)
    logical_pointers = _logical_image_pointers(settings)
    target_uuid = _target_uuid(target)
    stale_images = tuple(
        image
        for image in bpy.data.images
        if _owned_image(image, {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE})
        and str(image.get(TARGET_UUID_KEY, "") or "") == target_uuid
        and _pointer(image) not in logical_pointers
        and not bool(image.get("pp_recomposite_pending_delete", False))
        and not bool(image.get("pp_layer_reset_pending_delete", False))
        and not bool(image.get(ORIGINAL_REFERENCE_PENDING_DELETE_KEY, False))
    )
    for image in stale_images:
        managed_node_users = sum(
            1
            for node in stale_nodes
            if getattr(node, "image", None) is image
        )
        if int(image.users) > managed_node_users:
            continue
        if bool(image.is_dirty):
            raise LayerShaderError("Dirty 고아 Paint Image는 자동 삭제할 수 없습니다.")
    for node in stale_nodes:
        tree.nodes.remove(node)
    for image in stale_images:
        if _pointer(image) and int(image.users) == 0:
            _remove_image(image)


def _frame_width(layer: Any) -> float:
    """Layer 보정 개수로 충돌 없는 Frame 예약 너비를 계산합니다."""
    return 720.0 + 520.0 * len(layer.adjustments)


def _unmanaged_bottom(tree: Any) -> float:
    """사용자 Node 전체의 예상 Bounding Box 아래쪽 좌표를 반환합니다."""
    bottoms = []
    for node in tree.nodes:
        if _owned_node(node) and str(node.get(RECORD_KIND_KEY, "")) in _MANAGED_NODE_ROLES:
            continue
        location = getattr(node, "location_absolute", node.location)
        height = float(node.dimensions.y) if float(node.dimensions.y) > 1.0 else 180.0
        bottoms.append(float(location.y) - height)
    return min(bottoms) if bottoms else 0.0


def layout_layer_nodes(
    material: Any,
    layers: tuple[Any, ...],
    nodes_by_layer: dict[str, tuple[Any, Any, Any]],
    adjustment_nodes: dict[str, Any],
    apply_mix_nodes: dict[str, Any],
    layer_mask_nodes: dict[str, tuple[Any, Any, Any]],
    adjustment_mask_nodes: dict[str, tuple[Any, Any]],
) -> None:
    """관리 Layer Frame을 사용자 Node 아래 빈 영역에 겹치지 않게 배치합니다."""
    tree = material.node_tree
    evaluation_order = tuple(reversed(layers))
    widths = [_frame_width(layer) for layer in evaluation_order]
    gap = 160.0
    total_width = sum(widths) + gap * max(0, len(widths) - 1)
    principled = _principled(material)
    cursor_x = float(principled.location.x) - total_width
    frame_y = _unmanaged_bottom(tree) - 320.0
    for layer, width in zip(evaluation_order, widths):
        layer_uuid = str(layer.layer_uuid)
        frame, image_node, mix_node = nodes_by_layer[layer_uuid]
        frame.location = (cursor_x, frame_y)
        image_node.location = (40.0, -80.0)
        image_node.parent = frame
        x = 340.0
        for adjustment in layer.adjustments:
            adjustment_uuid = str(adjustment.adjustment_uuid)
            node = adjustment_nodes[adjustment_uuid]
            apply_mix = apply_mix_nodes[adjustment_uuid]
            node.parent = frame
            apply_mix.parent = frame
            node.location = (x, -80.0)
            apply_mix.location = (x + 260.0, -80.0)
            mask_uuid = str(adjustment.mask.mask_uuid or "")
            if mask_uuid:
                image, bw = adjustment_mask_nodes[mask_uuid]
                image.parent = frame
                bw.parent = frame
                image.location = (x, -420.0)
                bw.location = (x + 300.0, -420.0)
            x += 520.0
        layer_mask_uuid = str(layer.mask.mask_uuid or "")
        if layer_mask_uuid:
            image, bw, multiply = layer_mask_nodes[layer_mask_uuid]
            image.parent = frame
            bw.parent = frame
            multiply.parent = frame
            image.location = (40.0, -760.0)
            bw.location = (340.0, -760.0)
            multiply.location = (570.0, -760.0)
        mix_node.parent = frame
        mix_node.location = (width - 240.0, -80.0)
        cursor_x += width + gap


def _mask_preview_source(material: Any) -> Any | None:
    """Material 태그와 일치하는 유효한 Mask 흑백 출력 Socket을 반환합니다."""
    preview_uuid = str(material.get(MASK_PREVIEW_UUID_KEY, "") or "")
    if not preview_uuid:
        return None
    tree = material.node_tree
    matches = tuple(
        node
        for node in tree.nodes
        if _owned_node(node)
        and str(node.get(RECORD_KIND_KEY, ""))
        in {MASK_BW_NODE_ROLE, ADJUSTMENT_MASK_BW_NODE_ROLE}
        and str(node.get(MASK_UUID_KEY, "")) == preview_uuid
    )
    if len(matches) != 1:
        raise LayerShaderError("Mask 미리보기 흑백 Node를 하나로 찾을 수 없습니다.")
    color_links = matches[0].inputs["Color"].links
    if (
        len(color_links) != 1
        or not _owned_node(color_links[0].from_node)
        or str(color_links[0].from_node.get(RECORD_KIND_KEY, ""))
        not in {MASK_IMAGE_NODE_ROLE, ADJUSTMENT_MASK_IMAGE_NODE_ROLE}
        or str(color_links[0].from_node.get(MASK_UUID_KEY, ""))
        != preview_uuid
    ):
        raise LayerShaderError("Mask 미리보기 Image 연결이 유효하지 않습니다.")
    return matches[0].outputs["Val"]


def _ensure_layer_solo_preview_mix(tree: Any) -> Any:
    """레이어 개인 단독 보기에 사용하는 애드온 소유 Mix Node 하나를 준비합니다."""
    matches = _nodes_with_role(tree, LAYER_SOLO_PREVIEW_MIX_NODE_ROLE)
    if len(matches) > 1:
        raise LayerShaderError("Layer 개인 단독 보기 Mix Node가 둘 이상 존재합니다.")
    preview = matches[0] if matches else _new_node(
        tree,
        "ShaderNodeMixRGB",
        LAYER_SOLO_PREVIEW_MIX_NODE_ROLE,
        "",
    )
    preview.name = "PP Layer Solo Preview Mix"
    preview.label = "Layer Solo Preview"
    preview.blend_type = "MIX"
    preview.width = 180.0
    preview.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)
    return preview


def _copy_input_socket(tree: Any, source: Any, target: Any) -> None:
    """원본 입력 Socket의 단일 링크 또는 기본값을 대상 입력에 그대로 복제합니다."""
    if len(source.links) > 1:
        raise LayerShaderError("복제할 Layer 입력 연결이 하나를 초과합니다.")
    if source.links:
        _connect(tree, source.links[0].from_socket, target)
        return
    _clear_input(tree, target)
    target.default_value = source.default_value


def _sync_layer_solo_preview_mix(material: Any, layer_uuid: str) -> Any:
    """선택 Layer의 처리 색과 Factor를 검정 Base 전용 Preview Mix에 동기화합니다."""
    tree = material.node_tree
    layer_mix = _node_for(tree, LAYER_MIX_NODE_ROLE, layer_uuid)
    if layer_mix is None:
        raise LayerShaderError("Layer 개인 단독 보기의 원본 Mix Node를 찾을 수 없습니다.")
    preview = _ensure_layer_solo_preview_mix(tree)
    _tag_node(preview, LAYER_SOLO_PREVIEW_MIX_NODE_ROLE, layer_uuid)
    _clear_input(tree, preview.inputs[1])
    preview.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)
    _copy_input_socket(tree, layer_mix.inputs[2], preview.inputs[2])
    _copy_input_socket(tree, layer_mix.inputs[0], preview.inputs[0])
    return preview


def _same_input_socket(source: Any, target: Any) -> bool:
    """두 입력 Socket이 같은 링크 또는 같은 기본값을 사용하는지 확인합니다."""
    if len(source.links) != len(target.links) or len(source.links) > 1:
        return False
    if source.links:
        return _pointer(source.links[0].from_socket) == _pointer(
            target.links[0].from_socket
        )
    try:
        return tuple(source.default_value) == tuple(target.default_value)
    except TypeError:
        return float(source.default_value) == float(target.default_value)


def _layer_preview_source(material: Any) -> Any | None:
    """Material 태그와 일치하는 검증된 Layer 개인 단독 보기 출력을 반환합니다."""
    layer_uuid = str(material.get(LAYER_PREVIEW_UUID_KEY, "") or "")
    if not layer_uuid:
        return None
    tree = material.node_tree
    layer_mix = _node_for(tree, LAYER_MIX_NODE_ROLE, layer_uuid)
    matches = _nodes_with_role(tree, LAYER_SOLO_PREVIEW_MIX_NODE_ROLE)
    if layer_mix is None or len(matches) != 1:
        raise LayerShaderError("Layer 개인 단독 보기 Mix를 하나로 찾을 수 없습니다.")
    preview = matches[0]
    if preview.type != "MIX_RGB" or str(preview.blend_type) != "MIX":
        raise LayerShaderError("Layer 개인 단독 보기 Node 타입이 유효하지 않습니다.")
    if str(preview.get(LAYER_UUID_KEY, "") or "") != layer_uuid:
        raise LayerShaderError("Layer 개인 단독 보기 UUID가 Material 태그와 다릅니다.")
    if (
        len(preview.inputs[1].links) != 0
        or tuple(preview.inputs[1].default_value) != (0.0, 0.0, 0.0, 1.0)
    ):
        raise LayerShaderError("Layer 개인 단독 보기 Base가 검정이 아닙니다.")
    if not _same_input_socket(layer_mix.inputs[2], preview.inputs[2]):
        raise LayerShaderError("Layer 개인 단독 보기 처리 색 연결이 원본 Layer와 다릅니다.")
    if not _same_input_socket(layer_mix.inputs[0], preview.inputs[0]):
        raise LayerShaderError("Layer 개인 단독 보기 Factor 연결이 원본 Layer와 다릅니다.")
    return preview.outputs["Color"]


def _original_preview_source(material: Any) -> Any | None:
    """Original 단독 보기 태그가 가리키는 Raw Base Color Socket을 반환합니다."""
    if not bool(material.get(ORIGINAL_PREVIEW_KEY, False)):
        return None
    gate = _original_gate(material.node_tree)
    if gate is None:
        raise LayerShaderError("Original 단독 보기 Gate를 찾을 수 없습니다.")
    source, _default = _original_gate_raw_input(gate)
    return source


def _original_preview_uses_default(material: Any) -> bool:
    """Original 단독 보기가 링크 없는 저장 Base Color 기본값을 쓰는지 확인합니다."""
    if not bool(material.get(ORIGINAL_PREVIEW_KEY, False)):
        return False
    gate = _original_gate(material.node_tree)
    if gate is None:
        raise LayerShaderError("Original 단독 보기 Gate를 찾을 수 없습니다.")
    source, _default = _original_gate_raw_input(gate)
    return source is None


def _preview_source(material: Any) -> Any | None:
    """전역 단독 보기 태그와 일치하는 유일한 출력 Socket을 반환합니다."""
    layer_source = _layer_preview_source(material)
    mask_source = _mask_preview_source(material)
    original_active = bool(material.get(ORIGINAL_PREVIEW_KEY, False))
    original_source = _original_preview_source(material)
    if sum(
        (
            layer_source is not None,
            mask_source is not None,
            original_active,
        )
    ) > 1:
        raise LayerShaderError("Layer, Mask, Original 단독 보기는 동시에 활성화할 수 없습니다.")
    if original_active:
        return original_source
    return layer_source if layer_source is not None else mask_source


def _top_mix_node(mixes: tuple[Any, ...]) -> Any:
    """단일 Layer Chain에서 다른 Mix 입력으로 소비되지 않는 최상단 Mix를 반환합니다."""
    mix_pointers = {_pointer(node) for node in mixes}
    lower_pointers = {
        _pointer(link.from_node)
        for node in mixes
        for link in node.outputs["Color"].links
        if _pointer(link.to_node) in mix_pointers
        and _pointer(link.to_socket) == _pointer(link.to_node.inputs[1])
    }
    tops = tuple(node for node in mixes if _pointer(node) not in lower_pointers)
    if len(tops) != 1:
        raise LayerShaderError("Layer Chain의 최상단 Mix를 하나로 찾을 수 없습니다.")
    return tops[0]


def validate_layer_graph(
    material: Any,
    expected_layer_uuids: tuple[str, ...],
    *,
    expected_original_base: bool | None = None,
    expected_original_visible: bool | None = None,
    allow_legacy_gate: bool = False,
) -> None:
    """검정 Base에서 시작하는 단일 Layer Chain인지 fail-closed로 검증합니다."""
    tree = material.node_tree
    mixes = _nodes_with_role(tree, LAYER_MIX_NODE_ROLE)
    if not mixes:
        raise LayerShaderError("관리 Layer Mix Node가 없습니다.")
    if expected_original_base is not None and not isinstance(
        expected_original_base,
        bool,
    ):
        raise LayerShaderError("Original Base 기대 상태가 유효하지 않습니다.")
    if expected_original_visible is not None and not isinstance(
        expected_original_visible,
        bool,
    ):
        raise LayerShaderError("Original Layer 눈 기대 상태가 유효하지 않습니다.")
    if expected_original_base is True:
        raise LayerShaderError("구형 Original Base Color Gate 구조는 더 이상 지원하지 않습니다.")
    if expected_original_visible is not None and expected_original_base is not False:
        raise LayerShaderError("Original Layer 눈 상태는 일반 Layer 체인에서 검증해야 합니다.")
    gate = _original_gate(tree)
    if gate is not None and not bool(allow_legacy_gate):
        raise LayerShaderError("구형 Original Base Color Gate가 남아 있습니다.")
    expected = tuple(str(value) for value in expected_layer_uuids)
    if not expected or any(not value for value in expected):
        raise LayerShaderError("기대 Layer UUID가 비어 있습니다.")
    if len(set(expected)) != len(expected):
        raise LayerShaderError("기대 Layer UUID가 중복됩니다.")
    expected_mixes = tuple(
        _node_for(tree, LAYER_MIX_NODE_ROLE, layer_uuid)
        for layer_uuid in expected
    )
    if any(node is None for node in expected_mixes):
        raise LayerShaderError("기대 Layer Mix Node를 모두 찾을 수 없습니다.")
    principled = _principled(material)
    base = principled.inputs["Base Color"]
    preview = _preview_source(material)
    original_default_preview = _original_preview_uses_default(material)
    if len(base.links) != 1 and not original_default_preview:
        raise LayerShaderError("Principled Base Color 연결이 하나가 아닙니다.")
    if preview is not None:
        if _pointer(base.links[0].from_socket) != _pointer(preview):
            raise LayerShaderError("단독 미리보기가 Principled Base Color와 다릅니다.")
        current = expected_mixes[0]
    elif original_default_preview:
        raise LayerShaderError("구형 Original Layer 단독 보기 상태가 남아 있습니다.")
    else:
        if base.links[0].from_node not in mixes:
            raise LayerShaderError("Layer Stack 출력이 Principled Base Color와 다릅니다.")
        current = base.links[0].from_node
        if expected_mixes and _pointer(current) != _pointer(expected_mixes[0]):
            raise LayerShaderError("기대 최상단 Layer와 Base Color 출력이 다릅니다.")
    expected_pointers = tuple(_pointer(node) for node in expected_mixes)
    for index, pointer in enumerate(expected_pointers):
        if _pointer(current) != pointer:
            raise LayerShaderError("기대 Layer 순서와 Mix Chain이 다릅니다.")
        input_socket = current.inputs[1]
        if index + 1 < len(expected_pointers):
            if (
                len(input_socket.links) != 1
                or _pointer(input_socket.links[0].from_node)
                != expected_pointers[index + 1]
            ):
                raise LayerShaderError("기대 Layer Mix Chain이 중간에서 끊어졌습니다.")
            current = input_socket.links[0].from_node
        elif (
            len(input_socket.links) == 1
            and _owned_node(
                input_socket.links[0].from_node,
                LAYER_MIX_NODE_ROLE,
            )
        ):
            raise LayerShaderError("기대 최하단 Layer 아래에 관리 Mix가 남아 있습니다.")


    bottom_input = current.inputs[1]
    if expected_original_base is False:
        if len(bottom_input.links) != 0 or tuple(bottom_input.default_value) != (
            0.0,
            0.0,
            0.0,
            1.0,
        ):
            raise LayerShaderError("Composite 최하단 Layer는 검정 Base에서 시작해야 합니다.")
    elif len(bottom_input.links) != 0 or tuple(bottom_input.default_value) != (
        0.0,
        0.0,
        0.0,
        1.0,
    ):
        raise LayerShaderError("최하단 Layer는 검정 Base에서 시작해야 합니다.")


def validate_active_layer_graph(
    scene: Any,
    material: Any = None,
    *,
    allow_legacy_gate: bool = False,
) -> None:
    """Scene 상태의 활성 출력 Chain만 엄격하게 검증합니다."""
    settings = _settings(scene)
    if settings is None:
        raise LayerShaderError("Projection Paint Scene 설정이 없습니다.")
    active_material = material or getattr(settings, "paint_material", None)
    if active_material is None or active_material.node_tree is None:
        raise LayerShaderError("활성 Layer Graph를 검증할 관리 Material이 없습니다.")
    scene_preview = (
        str(settings.layer_preview_uuid or ""),
        str(settings.mask_preview_adjustment_uuid or ""),
        bool(settings.original_layer_preview),
    )
    material_preview = (
        str(active_material.get(LAYER_PREVIEW_UUID_KEY, "") or ""),
        str(active_material.get(MASK_PREVIEW_UUID_KEY, "") or ""),
        bool(active_material.get(ORIGINAL_PREVIEW_KEY, False)),
    )
    if scene_preview != material_preview:
        raise LayerShaderError("Scene 선택과 Material 단독 보기 태그가 다릅니다.")
    if sum((bool(scene_preview[0]), bool(scene_preview[1]), scene_preview[2])) > 1:
        raise LayerShaderError("Scene 단독 보기 상태는 동시에 하나만 활성화할 수 있습니다.")
    graph_layers = active_base_color_graph_layers(settings)
    if not graph_layers:
        raise LayerShaderError("활성 출력 Layer Chain이 비어 있습니다.")
    validate_layer_graph(
        active_material,
        tuple(str(layer.layer_uuid) for layer in graph_layers),
        expected_original_base=False,
        allow_legacy_gate=allow_legacy_gate,
    )


def _rebuild_layer_chain(
    material: Any,
    layers: tuple[Any, ...],
    nodes_by_layer: dict[str, tuple[Any, Any, Any]],
    base_source: Any | None,
    base_default: tuple[float, float, float, float],
) -> None:
    """목록 아래에서 위 순서로 Layer Mix 전체 Chain을 다시 연결합니다."""
    if not layers:
        raise LayerShaderError("연결할 Paint Layer가 없습니다.")
    tree = material.node_tree
    for layer in layers:
        mix_node = nodes_by_layer[str(layer.layer_uuid)][2]
        _clear_input(tree, mix_node.inputs[1])
    previous = base_source
    for layer in reversed(layers):
        mix_node = nodes_by_layer[str(layer.layer_uuid)][2]
        if previous is None:
            mix_node.inputs[1].default_value = base_default
        else:
            _connect(tree, previous, mix_node.inputs[1])
        previous = mix_node.outputs["Color"]
    principled = _principled(material)
    _connect(tree, previous, principled.inputs["Base Color"])


def connect_new_layer_nodes(scene: Any, layer: Any) -> Any:
    """새 Layer의 Image Texture와 Mix 노드만 기존 합성 체인 앞에 연결합니다."""
    settings = _settings(scene)
    material = getattr(settings, "paint_material", None)
    target = getattr(settings, "paint_target", None)
    if material is None or material.node_tree is None or target is None:
        raise LayerShaderError("새 Layer를 연결할 관리 Material이 없습니다.")
    reference = _managed_layer_reference(
        settings,
        exclude_layer_uuid=str(layer.layer_uuid),
    )
    if reference is None:
        raise LayerShaderError("새 Layer 해상도의 기준 관리 Image가 없습니다.")
    size = _image_size(reference)
    _validate_target_image_contract(target, reference, size, reference)
    new_image = _new_image(
        target,
        _layer_image_name(target, layer),
        size,
        role=LAYER_IMAGE_ROLE,
        layer_uuid=str(layer.layer_uuid),
        color=(0.0, 0.0, 0.0, 0.0),
        reference=reference,
    )
    _validate_target_image_contract(target, new_image, size, reference)
    layer.image = new_image
    tree = material.node_tree
    base = _principled(material).inputs["Base Color"]
    display_source = base.links[0].from_socket if base.links else None
    previous_layer = next(
        (
            candidate
            for candidate in settings.layers
            if str(candidate.layer_uuid) != str(layer.layer_uuid)
        ),
        None,
    )
    previous_mix = (
        _node_for(tree, LAYER_MIX_NODE_ROLE, str(previous_layer.layer_uuid))
        if previous_layer is not None
        else None
    )
    previous = previous_mix.outputs["Color"] if previous_mix is not None else display_source
    try:
        _frame, image_node, mix_node = _ensure_layer_nodes(tree, layer)
        _connect(tree, image_node.outputs["Color"], mix_node.inputs[2])
        _connect(tree, image_node.outputs["Alpha"], mix_node.inputs[0])
        if previous is None:
            mix_node.inputs[1].default_value = tuple(base.default_value)
        else:
            _connect(tree, previous, mix_node.inputs[1])
        _connect(tree, mix_node.outputs["Color"], base)
        _apply_preview(scene, material)
        return image_node
    except Exception:
        layer.image = None
        for node in tuple(
            node
            for node in tree.nodes
            if _owned_node(node)
            and str(node.get(LAYER_UUID_KEY, "")) == str(layer.layer_uuid)
        ):
            tree.nodes.remove(node)
        if display_source is not None:
            _connect(tree, display_source, base)
        _remove_image(new_image)
        raise


def add_layer_transaction(
    context: Any,
    layer_name: str,
    layer_uuid: str,
) -> Any:
    """새 논리 Layer와 Image·Node·Link·선택 Pointer를 한 Transaction으로 추가합니다."""
    scene = context.scene
    settings = _settings(scene)
    material = getattr(settings, "paint_material", None) if settings is not None else None
    if settings is None:
        raise LayerShaderError("새 Layer를 추가할 Scene 설정이 없습니다.")
    if composite_state_locks_layer_edits(settings):
        raise LayerShaderError("Composite 처리 중이거나 손상된 상태에서는 Layer를 추가할 수 없습니다.")

    def insertion_index() -> int:
        """새 일반 Layer를 Composite 상태와 무관하게 최상단에 삽입합니다."""
        return 0

    if material is None:
        layer = settings.layers.add()
        layer.name = str(layer_name)
        layer.layer_uuid = str(layer_uuid)
        layer.layer_role = NORMAL_LAYER_ROLE
        layer.composite_session_uuid = ""
        layer.composite_generation = 0
        new_index = len(settings.layers) - 1
        target_index = insertion_index()
        if new_index != target_index:
            settings.layers.move(new_index, target_index)
        settings.active_layer_index = target_index
        settings.active_mask_uuid = ""
        settings.paint_mask_mode = False
        return None
    if material.node_tree is None:
        raise LayerShaderError("새 Layer를 추가할 관리 Material이 없습니다.")
    tree = material.node_tree
    base = _principled(material).inputs["Base Color"]
    base_source = base.links[0].from_socket if base.links else None
    base_default = tuple(base.default_value)
    active_node = tree.nodes.active
    selected_nodes = tuple((node, bool(node.select)) for node in tree.nodes)
    snapshot = (
        int(settings.active_layer_index),
        str(settings.active_mask_uuid or ""),
        settings.overlay_image,
        settings.active_paint_image,
        bool(settings.paint_mask_mode),
        str(settings.layer_preview_uuid or ""),
        str(settings.mask_preview_adjustment_uuid or ""),
        int(material.paint_active_slot),
    )
    layer = settings.layers.add()
    layer.name = str(layer_name)
    layer.layer_uuid = str(layer_uuid)
    layer.layer_role = NORMAL_LAYER_ROLE
    layer.composite_session_uuid = ""
    layer.composite_generation = 0
    new_index = len(settings.layers) - 1
    target_index = insertion_index()
    if new_index != target_index:
        settings.layers.move(new_index, target_index)
    layer = settings.layers[target_index]
    new_image = None
    try:
        connect_new_layer_nodes(scene, layer)
        new_image = layer.image
        if composite_session_is_active(settings):
            reconcile_layer_graph(scene)
        else:
            validate_layer_graph(
                material,
                tuple(str(candidate.layer_uuid) for candidate in settings.layers),
                expected_original_base=False,
            )
        activate_layer_paint_image(context, str(layer.layer_uuid))
        return layer.image
    except Exception:
        failed = find_layer(settings, str(layer_uuid))
        if failed is not None:
            new_image = failed.image or new_image
            failed.image = None
        for node in tuple(
            node
            for node in tree.nodes
            if _owned_node(node)
            and str(node.get(LAYER_UUID_KEY, "")) == str(layer_uuid)
        ):
            tree.nodes.remove(node)
        failed_index = next(
            (
                index
                for index, candidate in enumerate(settings.layers)
                if str(candidate.layer_uuid) == str(layer_uuid)
            ),
            -1,
        )
        if failed_index >= 0:
            settings.layers.remove(failed_index)
        _clear_input(tree, base)
        if base_source is None:
            base.default_value = base_default
        else:
            tree.links.new(base_source, base)
        (
            settings.active_layer_index,
            settings.active_mask_uuid,
            settings.overlay_image,
            settings.active_paint_image,
            settings.paint_mask_mode,
            settings.layer_preview_uuid,
            settings.mask_preview_adjustment_uuid,
            previous_slot,
        ) = snapshot
        material.paint_active_slot = previous_slot
        for node, selected in selected_nodes:
            if _pointer(node):
                node.select = selected
        if _pointer(active_node):
            tree.nodes.active = active_node
        if _pointer(new_image) and int(new_image.users) == 0:
            _remove_image(new_image)
        _NODE_CACHE.clear()
        raise


def _clear_preview_tags(material: Any) -> None:
    """Material의 Layer·Mask 단독 보기 식별 태그를 안전하게 제거합니다."""
    for key in (
        LAYER_PREVIEW_UUID_KEY,
        MASK_PREVIEW_UUID_KEY,
        ORIGINAL_PREVIEW_KEY,
    ):
        if key in material:
            del material[key]


def _clear_preview_state(settings: Any, material: Any) -> None:
    """Scene과 Material의 전역 단독 보기 상태를 모두 해제합니다."""
    settings.layer_preview_uuid = ""
    settings.mask_preview_adjustment_uuid = ""
    settings.original_layer_preview = False
    _clear_preview_tags(material)


def _apply_preview(
    scene: Any,
    material: Any = None,
    allowed_layer_uuids: set[str] | None = None,
    allowed_mask_uuids: set[str] | None = None,
    top_layer_uuid: str = "",
) -> bool:
    """선택 Layer 개인 출력 또는 Mask 원본을 Base Color 대체 출력으로 적용합니다."""
    settings = _settings(scene)
    active_material = material or getattr(settings, "paint_material", None)
    if settings is None or active_material is None or active_material.node_tree is None:
        return False
    layers = tuple(settings.layers)
    if not layers:
        _clear_preview_state(settings, active_material)
        return False
    top_mix = _node_for(
        active_material.node_tree,
        LAYER_MIX_NODE_ROLE,
        str(top_layer_uuid or layers[0].layer_uuid),
    )
    if top_mix is None:
        raise LayerShaderError("최상단 Layer Mix Node를 찾을 수 없습니다.")
    principled = _principled(active_material)
    gate = _original_gate(active_material.node_tree)
    _connect(
        active_material.node_tree,
        top_mix.outputs["Color"],
        principled.inputs["Base Color"],
    )
    _clear_preview_tags(active_material)
    layer_uuid = str(settings.layer_preview_uuid or "")
    mask_uuid = str(settings.mask_preview_adjustment_uuid or "")
    original_preview = bool(settings.original_layer_preview)
    if sum((bool(layer_uuid), bool(mask_uuid), original_preview)) > 1:
        _clear_preview_state(settings, active_material)
        return False
    if original_preview:
        if gate is None:
            _clear_preview_state(settings, active_material)
            return False
        raw_source, raw_default = _original_gate_raw_input(gate)
        active_material[ORIGINAL_PREVIEW_KEY] = True
        if raw_source is None:
            _clear_input(active_material.node_tree, principled.inputs["Base Color"])
            principled.inputs["Base Color"].default_value = raw_default
        else:
            _connect(
                active_material.node_tree,
                raw_source,
                principled.inputs["Base Color"],
            )
        return True
    if layer_uuid:
        if (
            allowed_layer_uuids is not None
            and layer_uuid not in allowed_layer_uuids
        ):
            _clear_preview_state(settings, active_material)
            return False
        layer = find_layer(settings, layer_uuid)
        if layer is None:
            _clear_preview_state(settings, active_material)
            return False
        preview_mix = _sync_layer_solo_preview_mix(active_material, layer_uuid)
        active_material[LAYER_PREVIEW_UUID_KEY] = layer_uuid
        _connect(
            active_material.node_tree,
            preview_mix.outputs["Color"],
            principled.inputs["Base Color"],
        )
        return True
    if not mask_uuid:
        return False
    if (
        allowed_mask_uuids is not None
        and mask_uuid not in allowed_mask_uuids
    ):
        _clear_preview_state(settings, active_material)
        return False
    layer, adjustment, mask = find_mask(settings, mask_uuid)
    role = (
        ADJUSTMENT_MASK_BW_NODE_ROLE
        if adjustment is not None
        else MASK_BW_NODE_ROLE
    )
    preview = (
        _node_for_mask(
            active_material.node_tree,
            role,
            str(layer.layer_uuid),
            mask_uuid,
            str(adjustment.adjustment_uuid) if adjustment is not None else "",
        )
        if layer is not None
        else None
    )
    if mask is None or preview is None:
        _clear_preview_state(settings, active_material)
        return False
    active_material[MASK_PREVIEW_UUID_KEY] = mask_uuid
    _connect(
        active_material.node_tree,
        preview.outputs["Val"],
        principled.inputs["Base Color"],
    )
    return True


def toggle_layer_preview(scene: Any, layer_uuid: str) -> bool:
    """지정 Layer 개인 출력을 토글하고 Mask 단독 보기와 전체 합성을 함께 관리합니다."""
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        raise LayerShaderError("Layer 미리보기를 적용할 관리 Material이 없습니다.")
    if find_layer(settings, layer_uuid) is None:
        raise LayerShaderError("미리보기할 Layer를 찾을 수 없습니다.")
    material = settings.paint_material
    validate_active_layer_graph(scene, material)
    previous_layer = str(settings.layer_preview_uuid or "")
    previous_mask = str(settings.mask_preview_adjustment_uuid or "")
    previous_original = bool(settings.original_layer_preview)
    settings.layer_preview_uuid = (
        "" if previous_layer == str(layer_uuid) else str(layer_uuid)
    )
    settings.mask_preview_adjustment_uuid = ""
    settings.original_layer_preview = False
    try:
        enabled = _apply_preview(scene, material)
        validate_active_layer_graph(scene, material)
        return enabled
    except (AttributeError, RuntimeError, TypeError, ValueError):
        settings.layer_preview_uuid = previous_layer
        settings.mask_preview_adjustment_uuid = previous_mask
        settings.original_layer_preview = previous_original
        _apply_preview(scene, material)
        validate_active_layer_graph(scene, material)
        raise


def toggle_mask_preview(
    scene: Any,
    layer_uuid: str,
    mask_uuid: str,
) -> bool:
    """지정 Mask 단독 보기를 토글하고 Layer 체크와 이전 연결을 함께 관리합니다."""
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        raise LayerShaderError("Mask 미리보기를 적용할 관리 Material이 없습니다.")
    layer = find_layer(settings, layer_uuid)
    owner_layer, _adjustment, mask = find_mask(settings, mask_uuid)
    if (
        layer is None
        or owner_layer is None
        or _pointer(layer) != _pointer(owner_layer)
        or mask is None
    ):
        raise LayerShaderError("미리보기할 Mask를 찾을 수 없습니다.")
    material = settings.paint_material
    validate_active_layer_graph(scene, material)
    previous_layer = str(settings.layer_preview_uuid or "")
    previous_mask = str(settings.mask_preview_adjustment_uuid or "")
    previous_original = bool(settings.original_layer_preview)
    settings.layer_preview_uuid = ""
    settings.mask_preview_adjustment_uuid = (
        "" if previous_mask == str(mask_uuid) else str(mask_uuid)
    )
    settings.original_layer_preview = False
    try:
        enabled = _apply_preview(scene, material)
        validate_active_layer_graph(scene, material)
        return enabled
    except (AttributeError, RuntimeError, TypeError, ValueError):
        settings.layer_preview_uuid = previous_layer
        settings.mask_preview_adjustment_uuid = previous_mask
        settings.original_layer_preview = previous_original
        _apply_preview(scene, material)
        validate_active_layer_graph(scene, material)
        raise


def has_layer_backend(material: Any) -> bool:
    """Material에 UUID 기반 Layer Frame이 하나 이상 있는지 확인합니다."""
    return bool(
        material is not None
        and material.node_tree is not None
        and _nodes_with_role(material.node_tree, LAYER_FRAME_ROLE)
    )


def reconcile_layer_graph(
    scene: Any,
    *,
    material: Any = None,
    overlay: Any = None,
) -> Any:
    """Original 이관 전후의 Layer Graph 재구성을 하나의 원자적 작업으로 처리합니다."""
    settings = _settings(scene)
    if settings is None:
        return _reconcile_layer_graph_impl(
            scene,
            material=material,
            overlay=overlay,
        )
    active_material = material or getattr(settings, "paint_material", None)
    state = str(getattr(settings, "composite_state", "NONE"))
    schema_version = int(getattr(settings, "original_layer_schema_version", 0))
    original_before = original_layer(settings)
    if state == "STALE":
        raise LayerShaderError(
            "손상된 Composite 상태에서는 Original Layer를 자동 이관하지 않습니다."
        )
    if schema_version < ORIGINAL_LAYER_SCHEMA_VERSION and state in {
        "CREATING",
        "RECOMPOSITING",
        "RELEASING",
    }:
        raise LayerShaderError(
            "Composite 처리 중에는 Original Layer를 자동 이관하지 않습니다."
        )
    reference_migration = _prepare_original_reference_migration(settings)
    legacy_source = None
    legacy_default = (0.0, 0.0, 0.0, 1.0)
    snapshot = None
    created_source_tag_node = None
    if (
        schema_version < ORIGINAL_LAYER_SCHEMA_VERSION
        and original_before is None
        and active_material is not None
        and active_material.node_tree is not None
    ):
        legacy_source, legacy_default = _original_base_input(
            active_material.node_tree,
            _nodes_with_role(active_material.node_tree, LAYER_MIX_NODE_ROLE),
        )
        snapshot = _original_migration_settings_snapshot(settings)
        created_source_tag_node = _new_original_source_tag_node(legacy_source)
    try:
        result = _reconcile_layer_graph_impl(
            scene,
            material=material,
            overlay=overlay,
        )
        _finish_original_reference_migration(reference_migration)
        return result
    except Exception:
        _rollback_original_reference_migration(settings, reference_migration)
        partial_original = original_layer(settings)
        if (
            snapshot is not None
            and partial_original is not None
            and int(getattr(settings, "original_layer_schema_version", 0))
            < ORIGINAL_LAYER_SCHEMA_VERSION
        ):
            try:
                _rollback_uncommitted_original_migration(
                    scene,
                    active_material,
                    partial_original,
                    legacy_source,
                    legacy_default,
                    snapshot,
                    created_source_tag_node,
                )
            except Exception as rollback_error:
                raise LayerShaderError(
                    "Original Layer 이관 실패 뒤 기존 상태를 복원하지 못했습니다: "
                    f"{rollback_error}"
                ) from rollback_error
        raise


def _reconcile_layer_graph_impl(
    scene: Any,
    *,
    material: Any = None,
    overlay: Any = None,
) -> Any:
    """Scene Layer 데이터를 Image와 단일 Base Color Shader Chain으로 동기화합니다."""
    settings = _settings(scene)
    if settings is None:
        raise LayerShaderError("Projection Paint Scene 설정이 없습니다.")
    target = settings.paint_target
    active_material = material or settings.paint_material
    active_overlay = overlay or settings.overlay_image
    if target is None or _pointer(target) == 0:
        raise LayerShaderError("Layer Graph를 연결할 Paint Target이 없습니다.")
    if active_material is None or active_material.node_tree is None:
        raise LayerShaderError("Layer Graph를 연결할 관리 Material이 없습니다.")
    if str(getattr(settings, "composite_state", "NONE")) == "STALE":
        raise LayerShaderError("손상된 Composite 상태에서는 Layer Shader Graph를 자동으로 재구성할 수 없습니다.")
    legacy_source, legacy_default = _original_base_input(
        active_material.node_tree,
        _nodes_with_role(active_material.node_tree, LAYER_MIX_NODE_ROLE),
    )
    layers = _ensure_layer_data(scene, target, active_overlay)
    reference = _managed_layer_reference(settings) or active_overlay
    (
        migrated_original,
        migrated_source,
        migrated_color,
        migrated_socket,
    ) = migrate_original_layer(
        scene,
        target,
        active_material,
        reference,
    )
    layers = tuple(settings.layers)
    if migrated_color is not None and bool(settings.original_layer_preview):
        settings.layer_preview_uuid = str(migrated_original.layer_uuid)
        settings.mask_preview_adjustment_uuid = ""
        settings.original_layer_preview = False
    _sync_paint_image_selection(scene, settings)
    graph_layers = _graph_layers(settings, layers)
    active_before_reconcile = _active_layer(settings)
    state = str(getattr(settings, "composite_state", "NONE"))
    if (
        state == "ACTIVE"
        and not composite_layer_is_editable(settings, active_before_reconcile)
    ) or (
        state == "RELEASING" and active_before_reconcile not in graph_layers
    ):
        fallback_layer = next(
            (
                layer
                for layer in graph_layers
                if state != "ACTIVE"
                or str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
                == COMPOSITE_LAYER_ROLE
            ),
            None,
        )
        if fallback_layer is None:
            raise LayerShaderError("Composite 복구에 사용할 활성 Layer를 찾을 수 없습니다.")
        settings.active_layer_index = next(
            index
            for index, layer in enumerate(layers)
            if str(layer.layer_uuid) == str(fallback_layer.layer_uuid)
        )
        settings.active_mask_uuid = ""
        settings.paint_mask_mode = False
        settings.active_paint_image = fallback_layer.image
    active = _active_layer(settings)
    if active is None:
        raise LayerShaderError("활성 Paint Layer가 없습니다.")
    writable_reference = (
        active.image
        if _owned_image(active.image, {LAYER_IMAGE_ROLE})
        else _managed_layer_reference(settings)
    )
    if writable_reference is None:
        raise LayerShaderError("페인팅할 관리 Layer Image가 없습니다.")
    settings.overlay_image = writable_reference
    if original_layer_is_read_only(settings, active):
        settings.active_paint_image = None
    elif not _pointer(settings.active_paint_image):
        settings.active_paint_image = active.image
    tree = active_material.node_tree
    legacy_image, legacy_mix = _legacy_nodes(tree)
    nodes_by_layer: dict[str, tuple[Any, Any, Any]] = {}
    layer_color_sources: dict[str, Any] = {}
    adjustment_nodes: dict[str, Any] = {}
    apply_mix_nodes: dict[str, Any] = {}
    layer_mask_nodes: dict[str, tuple[Any, Any, Any]] = {}
    adjustment_mask_nodes: dict[str, tuple[Any, Any]] = {}
    for index, layer in enumerate(layers):
        nodes_by_layer[str(layer.layer_uuid)] = _ensure_layer_nodes(
            tree,
            layer,
            legacy_image=legacy_image if index == 0 else None,
            legacy_mix=legacy_mix if index == 0 else None,
        )
        frame, image_node, _mix_node = nodes_by_layer[str(layer.layer_uuid)]
        composite_color = _ensure_composite_unpremultiply_nodes(
            tree,
            layer,
            frame,
            image_node,
        )
        if composite_color is not None:
            layer_color_sources[str(layer.layer_uuid)] = composite_color.outputs["Color"]
        layer_mask_uuid = str(layer.mask.mask_uuid or "")
        if layer_mask_uuid:
            layer_mask_nodes[layer_mask_uuid] = _ensure_layer_mask_nodes(
                tree,
                layer,
            )
        for adjustment in layer.adjustments:
            adjustment_uuid = str(adjustment.adjustment_uuid)
            adjustment_nodes[adjustment_uuid] = _ensure_adjustment_node(
                tree,
                layer,
                adjustment,
            )
            apply_mix_nodes[adjustment_uuid] = _ensure_adjustment_apply_mix(
                tree,
                layer,
                adjustment,
            )
            mask_uuid = str(adjustment.mask.mask_uuid or "")
            if mask_uuid:
                adjustment_mask_nodes[mask_uuid] = _ensure_adjustment_mask_nodes(
                    tree,
                    layer,
                    adjustment,
                )
    _reconcile_stale_resources(tree, settings, target)
    for layer in layers:
        layer_uuid = str(layer.layer_uuid)
        _frame, image_node, mix_node = nodes_by_layer[layer_uuid]
        _link_layer_color(
            tree,
            layer,
            image_node,
            mix_node,
            adjustment_nodes,
            apply_mix_nodes,
            adjustment_mask_nodes,
            layer_color_sources.get(layer_uuid),
        )
        _link_layer_factor(
            tree,
            layer,
            image_node,
            mix_node,
            layer_mask_nodes,
        )
    _rebuild_layer_chain(
        active_material,
        graph_layers,
        nodes_by_layer,
        None,
        (0.0, 0.0, 0.0, 1.0),
    )
    layout_layer_nodes(
        active_material,
        layers,
        nodes_by_layer,
        adjustment_nodes,
        apply_mix_nodes,
        layer_mask_nodes,
        adjustment_mask_nodes,
    )
    _apply_preview(
        scene,
        active_material,
        allowed_layer_uuids={str(layer.layer_uuid) for layer in graph_layers},
        allowed_mask_uuids={
            str(mask.mask_uuid)
            for layer in graph_layers
            for mask in (
                layer.mask,
                *(adjustment.mask for adjustment in layer.adjustments),
            )
            if str(mask.mask_uuid or "")
        },
        top_layer_uuid=str(graph_layers[0].layer_uuid),
    )
    validate_active_layer_graph(
        scene,
        active_material,
        allow_legacy_gate=migrated_color is not None,
    )
    original = original_layer(settings)
    settings.original_layer_name = str(original.name) if original is not None else "Original Layer"
    if str(getattr(target, "mode", "")) == "TEXTURE_PAINT":
        if bool(settings.paint_mask_mode) and str(settings.active_mask_uuid or ""):
            _layer, adjustment, mask = find_mask(
                settings,
                str(settings.active_mask_uuid),
            )
            if mask is None or not _pointer(mask.image):
                raise LayerShaderError("활성 Mask Paint Image를 찾을 수 없습니다.")
            _activate_paint_image_node(
                active_material,
                (
                    ADJUSTMENT_MASK_IMAGE_NODE_ROLE
                    if adjustment is not None
                    else MASK_IMAGE_NODE_ROLE
                ),
                mask.image,
            )
        elif not original_layer_is_read_only(settings, active):
            _activate_paint_image_node(
                active_material,
                LAYER_IMAGE_NODE_ROLE,
                active.image,
            )
    if migrated_color is not None:
        source_node_uuid = ""
        source_socket_identifier = ""
        if migrated_socket is not None:
            (
                source_node_uuid,
                source_socket_identifier,
            ) = _tag_original_source_socket(migrated_socket)
        settings.original_source_image = migrated_source
        settings.original_source_color = migrated_color
        settings.original_source_node_uuid = source_node_uuid
        settings.original_source_socket_identifier = source_socket_identifier
        settings.original_layer_preview = False
        _remove_original_gates(tree)
        settings.original_layer_schema_version = ORIGINAL_LAYER_SCHEMA_VERSION
    _NODE_CACHE.clear()
    return active.image


def _cached_tagged_node(
    material: Any,
    item_uuid: str,
    role: str,
    uuid_key: str,
) -> Any | None:
    """Runtime Cache 또는 소유 태그에서 UUID 역할 Node를 반환합니다."""
    key = (_pointer(material), str(item_uuid), str(role))
    node = _NODE_CACHE.get(key)
    if (
        node is not None
        and _pointer(node)
        and _owned_node(node, role)
        and str(node.get(uuid_key, "")) == str(item_uuid)
    ):
        return node
    matches = tuple(
        candidate
        for candidate in material.node_tree.nodes
        if _owned_node(candidate, role)
        and str(candidate.get(uuid_key, "")) == str(item_uuid)
    )
    if len(matches) != 1:
        return None
    _NODE_CACHE[key] = matches[0]
    return matches[0]


def _cached_adjustment_node(material: Any, adjustment_uuid: str) -> Any | None:
    """Runtime Cache 또는 Node 태그에서 Adjustment Node를 반환합니다."""
    return _cached_tagged_node(
        material,
        adjustment_uuid,
        ADJUSTMENT_NODE_ROLE,
        ADJUSTMENT_UUID_KEY,
    )


def update_adjustment(scene: Any, adjustment_uuid: str) -> None:
    """한 Adjustment의 값과 적용 Factor만 대응 Node에 반영합니다."""
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        return
    if str(getattr(settings, "composite_state", "NONE")) == "STALE":
        raise LayerShaderError("손상된 Composite 상태에서는 Adjustment를 갱신할 수 없습니다.")
    _layer, adjustment = find_adjustment(settings, adjustment_uuid)
    if adjustment is None or not has_layer_backend(settings.paint_material):
        return
    node = _cached_adjustment_node(
        settings.paint_material,
        adjustment_uuid,
    )
    if node is None:
        raise LayerShaderError("색상 보정 Node를 하나로 찾을 수 없습니다.")
    sync_adjustment_node(node, adjustment)
    apply_mix = _cached_tagged_node(
        settings.paint_material,
        adjustment_uuid,
        ADJUSTMENT_APPLY_MIX_NODE_ROLE,
        ADJUSTMENT_UUID_KEY,
    )
    if apply_mix is None:
        raise LayerShaderError("Adjustment Apply Mix를 하나로 찾을 수 없습니다.")
    mask_uuid = str(adjustment.mask.mask_uuid or "")
    mask_nodes = None
    if mask_uuid:
        image = _cached_tagged_node(
            settings.paint_material,
            mask_uuid,
            ADJUSTMENT_MASK_IMAGE_NODE_ROLE,
            MASK_UUID_KEY,
        )
        bw = _cached_tagged_node(
            settings.paint_material,
            mask_uuid,
            ADJUSTMENT_MASK_BW_NODE_ROLE,
            MASK_UUID_KEY,
        )
        if image is None or bw is None:
            raise LayerShaderError("Adjustment Mask Node를 찾을 수 없습니다.")
        mask_nodes = (image, bw)
    _sync_adjustment_apply_mix(
        settings.paint_material.node_tree,
        adjustment,
        apply_mix,
        mask_nodes,
    )


def update_mask(scene: Any, mask_uuid: str) -> None:
    """한 Layer 또는 Adjustment Mask의 눈 상태만 Shader Factor에 반영합니다."""
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        return
    if str(getattr(settings, "composite_state", "NONE")) == "STALE":
        raise LayerShaderError("손상된 Composite 상태에서는 Mask를 갱신할 수 없습니다.")
    layer, adjustment, mask = find_mask(settings, mask_uuid)
    material = settings.paint_material
    if mask is None or not has_layer_backend(material):
        return
    if adjustment is not None:
        update_adjustment(scene, str(adjustment.adjustment_uuid))
        return
    image_node = _cached_tagged_node(
        material,
        str(layer.layer_uuid),
        LAYER_IMAGE_NODE_ROLE,
        LAYER_UUID_KEY,
    )
    mix_node = _cached_tagged_node(
        material,
        str(layer.layer_uuid),
        LAYER_MIX_NODE_ROLE,
        LAYER_UUID_KEY,
    )
    image = _cached_tagged_node(
        material,
        mask_uuid,
        MASK_IMAGE_NODE_ROLE,
        MASK_UUID_KEY,
    )
    bw = _cached_tagged_node(
        material,
        mask_uuid,
        MASK_BW_NODE_ROLE,
        MASK_UUID_KEY,
    )
    multiply = _cached_tagged_node(
        material,
        mask_uuid,
        MASK_MULTIPLY_NODE_ROLE,
        MASK_UUID_KEY,
    )
    if any(node is None for node in (image_node, mix_node, image, bw, multiply)):
        raise LayerShaderError("Layer Mask Node를 모두 찾을 수 없습니다.")
    _link_layer_factor(
        material.node_tree,
        layer,
        image_node,
        mix_node,
        {mask_uuid: (image, bw, multiply)},
    )
    if str(settings.layer_preview_uuid or "") == str(layer.layer_uuid):
        _apply_preview(scene, material)


def update_layer_visibility(scene: Any, layer_uuid: str) -> None:
    """한 Layer의 눈 아이콘 상태를 Shader 합성 Factor에 반영합니다."""
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        return
    if str(getattr(settings, "composite_state", "NONE")) == "STALE":
        raise LayerShaderError("손상된 Composite 상태에서는 Layer 표시 상태를 갱신할 수 없습니다.")
    material = settings.paint_material
    layer = find_layer(settings, layer_uuid)
    if layer is None or not has_layer_backend(material):
        return
    image_node = _cached_tagged_node(
        material,
        str(layer.layer_uuid),
        LAYER_IMAGE_NODE_ROLE,
        LAYER_UUID_KEY,
    )
    mix_node = _cached_tagged_node(
        material,
        str(layer.layer_uuid),
        LAYER_MIX_NODE_ROLE,
        LAYER_UUID_KEY,
    )
    if image_node is None or mix_node is None:
        raise LayerShaderError("Layer 표시 상태를 갱신할 Node를 찾을 수 없습니다.")
    mask_nodes = {}
    mask_uuid = str(layer.mask.mask_uuid or "")
    if mask_uuid:
        image = _cached_tagged_node(
            material,
            mask_uuid,
            MASK_IMAGE_NODE_ROLE,
            MASK_UUID_KEY,
        )
        bw = _cached_tagged_node(
            material,
            mask_uuid,
            MASK_BW_NODE_ROLE,
            MASK_UUID_KEY,
        )
        multiply = _cached_tagged_node(
            material,
            mask_uuid,
            MASK_MULTIPLY_NODE_ROLE,
            MASK_UUID_KEY,
        )
        if any(node is None for node in (image, bw, multiply)):
            raise LayerShaderError("Layer Mask Node를 모두 찾을 수 없습니다.")
        mask_nodes[mask_uuid] = (image, bw, multiply)
    _link_layer_factor(
        material.node_tree,
        layer,
        image_node,
        mix_node,
        mask_nodes,
    )
    if str(settings.layer_preview_uuid or "") == str(layer.layer_uuid):
        _apply_preview(scene, material)


def _paint_image_slot_index(
    material: Any,
    image: Any,
    *,
    require_unique: bool = True,
) -> int:
    """지정 Image가 연결된 Material Texture Paint Slot을 검증해 반환합니다."""
    paint_images = tuple(material.texture_paint_images)
    image_pointer = _pointer(image)
    indices = tuple(
        index
        for index, candidate in enumerate(paint_images)
        if _pointer(candidate) == image_pointer
    )
    if not indices or (require_unique and len(indices) != 1):
        raise LayerShaderError("Paint Image가 Material Texture Paint Slot에 하나로 존재하지 않습니다.")
    return int(indices[0])


def _activate_paint_image_node(material: Any, role: str, image: Any) -> Any:
    """Image Node는 항상 활성화하고 실제 Paint Target일 때만 Slot을 검증합니다."""
    tree = material.node_tree
    matches = tuple(
        node
        for node in tree.nodes
        if _owned_node(node, role) and node.image is image
    )
    if len(matches) != 1:
        raise LayerShaderError("활성 Paint Image Texture Node를 하나로 찾을 수 없습니다.")
    for node in tree.nodes:
        if _owned_node(node) and str(node.get(RECORD_KIND_KEY, "")) in {
            LAYER_IMAGE_NODE_ROLE,
            MASK_IMAGE_NODE_ROLE,
            ADJUSTMENT_MASK_IMAGE_NODE_ROLE,
        }:
            node.select = False
    matches[0].select = True
    tree.nodes.active = matches[0]
    material.update_tag()
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()
    scene = getattr(bpy.context, "scene", None)
    settings = _settings(scene) if scene is not None else None
    target = getattr(settings, "paint_target", None) if settings is not None else None
    view_layer = getattr(bpy.context, "view_layer", None)
    actual_target = bool(
        _pointer(target)
        and str(getattr(target, "mode", "")) == "TEXTURE_PAINT"
        and _pointer(getattr(target, "active_material", None)) == _pointer(material)
        and view_layer is not None
        and getattr(view_layer.objects, "active", None) is target
    )
    if not actual_target:
        return matches[0]
    image_pointer = _pointer(image)
    material.paint_active_slot = _paint_image_slot_index(material, image)
    refreshed = tuple(material.texture_paint_images)
    active_index = int(material.paint_active_slot)
    if (
        active_index < 0
        or active_index >= len(refreshed)
        or _pointer(refreshed[active_index]) != image_pointer
    ):
        raise LayerShaderError("활성 Paint Slot과 선택 Layer Image가 일치하지 않습니다.")
    return matches[0]


def activate_layer_paint_image(context: Any, layer_uuid: str) -> Any:
    """일반 Layer는 Paint로, 외부 Original은 읽기 전용 선택으로 활성화합니다."""
    scene = context.scene
    settings = scene.projection_paint
    layer = find_layer(settings, layer_uuid)
    read_only_original = bool(
        layer is not None
        and _pointer(layer.image)
        and original_layer_is_read_only(settings, layer)
    )
    if layer is not None and _pointer(layer.image) and not read_only_original:
        try:
            from .overlay_migration import repair_managed_overlay_if_needed

            repair_managed_overlay_if_needed(context, overlay=layer.image)
        except RuntimeError as exc:
            raise LayerShaderError(f"Layer Image UDIM 복구 실패: {exc}") from exc
    if layer is None or not _pointer(layer.image):
        raise LayerShaderError("선택한 Layer의 Paint Image가 준비되지 않았습니다.")
    if not composite_layer_is_editable(settings, layer):
        raise LayerShaderError("Composite 활성 중에는 기존 Layer를 선택하거나 편집할 수 없습니다.")
    next_index = next(
        index
        for index, candidate in enumerate(settings.layers)
        if str(candidate.layer_uuid) == str(layer.layer_uuid)
    )
    snapshot = (
        int(settings.active_layer_index),
        int(layer.active_adjustment_index),
        str(settings.active_mask_uuid or ""),
        settings.overlay_image,
        settings.active_paint_image,
        bool(settings.paint_mask_mode),
        (
            int(settings.paint_material.paint_active_slot)
            if settings.paint_material is not None
            else -1
        ),
    )
    try:
        settings.active_layer_index = next_index
        layer.active_adjustment_index = -1
        settings.active_mask_uuid = ""
        settings.overlay_image = (
            _managed_layer_reference(settings)
            if read_only_original
            else layer.image
        )
        if settings.overlay_image is None:
            raise LayerShaderError("페인팅할 관리 Layer Image가 없습니다.")
        settings.active_paint_image = None if read_only_original else layer.image
        settings.paint_mask_mode = False
        if settings.paint_material is not None and not read_only_original:
            _activate_paint_image_node(
                settings.paint_material,
                LAYER_IMAGE_NODE_ROLE,
                layer.image,
            )
    except Exception:
        (
            settings.active_layer_index,
            layer.active_adjustment_index,
            settings.active_mask_uuid,
            settings.overlay_image,
            settings.active_paint_image,
            settings.paint_mask_mode,
            previous_slot,
        ) = snapshot
        if settings.paint_material is not None and previous_slot >= 0:
            settings.paint_material.paint_active_slot = previous_slot
        raise
    from .paint_runtime import resume_stencil_after_layer_selection

    resume_stencil_after_layer_selection(context)
    return layer.image


def activate_mask_paint_image(
    context: Any,
    layer_uuid: str,
    mask_uuid: str,
) -> Any:
    """선택 Mask의 Image Texture 노드와 Scene Pointer를 활성화합니다."""
    scene = context.scene
    settings = scene.projection_paint
    layer = find_layer(settings, layer_uuid)
    if layer is None:
        raise LayerShaderError("Mask가 속한 Layer를 찾을 수 없습니다.")
    read_only_original = original_layer_is_read_only(settings, layer)
    if _pointer(layer.image) and not read_only_original:
        try:
            from .overlay_migration import repair_managed_overlay_if_needed

            repair_managed_overlay_if_needed(context, overlay=layer.image)
        except RuntimeError as exc:
            raise LayerShaderError(f"Layer Image UDIM 복구 실패: {exc}") from exc
    if not composite_layer_is_editable(settings, layer):
        raise LayerShaderError("Composite 활성 중에는 기존 Layer Mask를 편집할 수 없습니다.")
    owner_layer, adjustment, mask = find_mask(settings, mask_uuid)
    if (
        owner_layer is None
        or _pointer(owner_layer) != _pointer(layer)
        or mask is None
        or not _pointer(mask.image)
    ):
        raise LayerShaderError("선택한 Mask Image가 준비되지 않았습니다.")
    settings.active_layer_index = next(
        index
        for index, candidate in enumerate(settings.layers)
        if str(candidate.layer_uuid) == str(layer.layer_uuid)
    )
    layer.active_adjustment_index = (
        next(
            index
            for index, candidate in enumerate(layer.adjustments)
            if str(candidate.adjustment_uuid) == str(adjustment.adjustment_uuid)
        )
        if adjustment is not None
        else -1
    )
    settings.active_mask_uuid = str(mask.mask_uuid)
    settings.overlay_image = (
        _managed_layer_reference(settings)
        if read_only_original
        else layer.image
    )
    if settings.overlay_image is None:
        raise LayerShaderError("Mask 페인팅에 필요한 관리 Layer Image가 없습니다.")
    settings.active_paint_image = mask.image
    settings.paint_mask_mode = True
    if settings.paint_material is not None:
        _activate_paint_image_node(
            settings.paint_material,
            (
                ADJUSTMENT_MASK_IMAGE_NODE_ROLE
                if adjustment is not None
                else MASK_IMAGE_NODE_ROLE
            ),
            mask.image,
        )
    from .stencil_session import disconnect_stencil

    disconnect_stencil(scene)
    return mask.image


def rename_layer_frame(scene: Any, layer_uuid: str) -> None:
    """Layer 이름을 대응 관리 Node Frame과 Image Node Label에 반영합니다."""
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        return
    layer = find_layer(settings, layer_uuid)
    if layer is None or not has_layer_backend(settings.paint_material):
        return
    tree = settings.paint_material.node_tree
    frame = _node_for(tree, LAYER_FRAME_ROLE, layer_uuid)
    image = _node_for(tree, LAYER_IMAGE_NODE_ROLE, layer_uuid)
    mix = _node_for(tree, LAYER_MIX_NODE_ROLE, layer_uuid)
    if frame is not None:
        frame.label = str(layer.name)
    if image is not None:
        image.label = str(layer.name)
    if mix is not None:
        mix.label = f"{layer.name} Normal"


def create_composite_layer_image(scene: Any, layer_uuid: str) -> Any:
    """기존 Layer UDIM 구성과 같은 빈 Composite Bake Image를 생성합니다."""
    settings = _settings(scene)
    target = getattr(settings, "paint_target", None)
    if settings is None or target is None or _pointer(target) == 0:
        raise LayerShaderError("Composite Image를 만들 Paint Target이 없습니다.")
    reference = _managed_layer_reference(settings)
    if not _pointer(reference):
        raise LayerShaderError("Composite Image 해상도의 기준 Layer가 없습니다.")
    image = _new_image(
        target,
        f"{target.name}_Composite_Paint",
        _image_size(reference),
        role=LAYER_IMAGE_ROLE,
        layer_uuid=str(layer_uuid),
        color=(0.0, 0.0, 0.0, 0.0),
        reference=reference,
    )
    _validate_target_image_contract(target, image, _image_size(reference), reference)
    image["pp_composite_session_uuid"] = str(
        getattr(settings, "active_composite_session_uuid", "") or ""
    )
    image[COMBINED_RGBA_IMAGE_KEY] = True
    return image


def remove_owned_layer_bundles(
    material: Any,
    layer_uuids: tuple[str, ...],
    images: tuple[Any, ...],
    *,
    detach_shared: bool = False,
) -> None:
    """검증된 Composite Layer 묶음의 관리 Node와 미참조 Image를 정리합니다."""
    if material is None or material.node_tree is None:
        raise LayerShaderError("Composite Layer 자원을 정리할 관리 Material이 없습니다.")
    expected = {str(value) for value in layer_uuids if str(value)}
    if not expected:
        raise LayerShaderError("정리할 Composite Layer UUID가 없습니다.")
    if len(expected) != len(tuple(layer_uuids)):
        raise LayerShaderError("정리할 Composite Layer UUID가 중복되었습니다.")
    for image in images:
        if not _owned_image(image, {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE}):
            raise LayerShaderError("Composite 삭제 대상 Image의 소유권이 유효하지 않습니다.")
        if str(image.get(LAYER_UUID_KEY, "") or "") not in expected:
            raise LayerShaderError("Composite 삭제 대상 Image UUID가 Layer와 다릅니다.")
    tree = material.node_tree
    for node in tuple(tree.nodes):
        if (
            _owned_node(node)
            and str(node.get(LAYER_UUID_KEY, "") or "") in expected
        ):
            tree.nodes.remove(node)
    for image in images:
        if _pointer(image) and int(image.users) == 0:
            _remove_image(image)
        elif detach_shared:
            detach_shared_owned_image(image)
    _NODE_CACHE.clear()


def detach_shared_owned_image(image: Any) -> None:
    """삭제된 Layer 밖에서 공유 중인 Image를 외부 데이터로 분리해 이후 정리 대상에서 제외합니다."""
    if not _owned_image(image, {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE}):
        return
    if _pointer(image) and int(getattr(image, "users", 0)) > 0:
        _replace_image_identity(image)


def _remove_image(image: Any) -> None:
    """참조 없는 관리 Image와 검증된 SINGLE·UDIM Staging만 제거합니다."""
    if not _owned_image(
        image,
        {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE},
    ):
        return
    from .overlay_durability import unregister_overlay
    from .image_backend import remove_managed_staging

    if _pointer(image) and int(getattr(image, "users", 0)) == 0:
        unregister_overlay(image)
        remove_managed_staging(image)
        bpy.data.images.remove(image)


def remove_adjustment_resources(
    scene: Any,
    layer: Any,
    adjustment: Any,
) -> None:
    """삭제할 Adjustment와 자체 Mask의 관리 자원만 제거합니다."""
    settings = _settings(scene)
    material = getattr(settings, "paint_material", None)
    adjustment_uuid = str(adjustment.adjustment_uuid)
    if str(adjustment.mask.mask_uuid or ""):
        remove_mask_resources(scene, layer, adjustment.mask, adjustment)
    if material is not None and material.node_tree is not None:
        tree = material.node_tree
        nodes = tuple(
            node
            for node in tree.nodes
            if _owned_node(node)
            and str(node.get(ADJUSTMENT_UUID_KEY, "")) == adjustment_uuid
        )
        for node in nodes:
            tree.nodes.remove(node)
    _NODE_CACHE.clear()


def remove_mask_resources(
    scene: Any,
    layer: Any,
    mask: Any,
    adjustment: Any = None,
) -> None:
    """삭제할 Layer 또는 Adjustment Mask의 Preview·Node·Image를 정리합니다."""
    settings = _settings(scene)
    material = getattr(settings, "paint_material", None)
    mask_uuid = str(mask.mask_uuid or "")
    if not mask_uuid:
        return
    if str(settings.mask_preview_adjustment_uuid or "") == mask_uuid:
        settings.mask_preview_adjustment_uuid = ""
        if material is not None and material.node_tree is not None:
            _apply_preview(scene, material)
    image = mask.image
    if str(settings.active_mask_uuid or "") == mask_uuid:
        settings.active_mask_uuid = ""
        read_only_original = original_layer_is_read_only(settings, layer)
        settings.active_paint_image = None if read_only_original else layer.image
        settings.paint_mask_mode = False
        if (
            material is not None
            and material.node_tree is not None
            and not read_only_original
        ):
            _activate_paint_image_node(material, LAYER_IMAGE_NODE_ROLE, layer.image)
    if material is not None and material.node_tree is not None:
        tree = material.node_tree
        nodes = tuple(
            node
            for node in tree.nodes
            if _owned_node(node)
            and str(node.get(MASK_UUID_KEY, "")) == mask_uuid
        )
        for node in nodes:
            tree.nodes.remove(node)
    mask.image = None
    mask.mask_uuid = ""
    _remove_image(image)
    detach_shared_owned_image(image)
    _NODE_CACHE.clear()


def _delete_owned_layer_resources(
    material: Any,
    layer_uuid: str,
    images: tuple[Any, ...],
) -> None:
    """새 Chain에서 제외된 Layer의 관리 Node와 미참조 Image만 정리합니다."""
    tree = material.node_tree
    nodes = tuple(
        node
        for node in tree.nodes
        if _owned_node(node)
        and str(node.get(LAYER_UUID_KEY, "")) == str(layer_uuid)
    )
    for node in nodes:
        tree.nodes.remove(node)
    for image in images:
        if not _owned_image(image, {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE}):
            continue
        if int(image.users) == 0:
            _remove_image(image)
        else:
            detach_shared_owned_image(image)
    _NODE_CACHE.clear()


def remove_layer_resources(context: Any, layer_uuid: str) -> Any:
    """생존 Layer를 활성화한 뒤 대상 Layer의 Link·Node·Image를 제거합니다."""
    scene = context.scene
    settings = _settings(scene)
    if settings is None or settings.paint_material is None:
        raise LayerShaderError("삭제할 Layer Graph가 준비되지 않았습니다.")
    material = settings.paint_material
    if material.node_tree is None or len(settings.layers) <= 1:
        raise LayerShaderError("Paint Material에는 Layer가 최소 한 개 필요합니다.")
    layers = tuple(settings.layers)
    deleted_index = next(
        (index for index, layer in enumerate(layers) if str(layer.layer_uuid) == str(layer_uuid)),
        -1,
    )
    if deleted_index < 0:
        raise LayerShaderError("삭제할 Paint Layer를 찾을 수 없습니다.")
    deleted = layers[deleted_index]
    survivor_index = min(deleted_index, len(layers) - 2)
    survivor = layers[survivor_index if survivor_index < deleted_index else survivor_index + 1]
    deleted_mask_uuids = {
        str(mask.mask_uuid)
        for mask in (deleted.mask, *(adjustment.mask for adjustment in deleted.adjustments))
        if str(mask.mask_uuid or "")
    }
    preview_was_deleted = (
        str(settings.layer_preview_uuid or "") == str(deleted.layer_uuid)
        or str(settings.mask_preview_adjustment_uuid or "") in deleted_mask_uuids
    )
    tree = material.node_tree
    mix = _node_for(tree, LAYER_MIX_NODE_ROLE, str(deleted.layer_uuid))
    if mix is None:
        raise LayerShaderError("삭제 Layer Mix Node를 찾을 수 없습니다.")
    incoming = mix.inputs[1].links[0].from_socket if mix.inputs[1].links else None
    incoming_default = tuple(mix.inputs[1].default_value)
    outgoing_sockets = tuple(link.to_socket for link in tuple(mix.outputs["Color"].links))
    activate_layer_paint_image(context, str(survivor.layer_uuid))
    for to_socket in outgoing_sockets:
        if incoming is None:
            _clear_input(tree, to_socket)
            to_socket.default_value = incoming_default
        else:
            _connect(tree, incoming, to_socket)
    deleted_images = tuple(
        image for image in (
            deleted.image,
            deleted.mask.image if str(deleted.mask.mask_uuid or "") else None,
            *(adjustment.mask.image for adjustment in deleted.adjustments if str(adjustment.mask.mask_uuid or "")),
        ) if _pointer(image)
    )
    settings.layers.remove(deleted_index)
    settings.active_layer_index = survivor_index
    settings.active_mask_uuid = ""
    settings.paint_mask_mode = False
    _delete_owned_layer_resources(material, str(layer_uuid), deleted_images)
    if preview_was_deleted:
        _clear_preview_tags(material)
        settings.layer_preview_uuid = ""
        settings.mask_preview_adjustment_uuid = ""
        top = settings.layers[0]
        top_mix = _node_for(tree, LAYER_MIX_NODE_ROLE, str(top.layer_uuid))
        _connect(tree, top_mix.outputs["Color"], _principled(material).inputs["Base Color"])
    return settings.layers[survivor_index].image


def managed_node_rectangles(material: Any) -> tuple[tuple[str, float, float, float, float], ...]:
    """테스트용 관리 Frame 예약 사각형을 절대 좌표로 반환합니다."""
    frames = _nodes_with_role(material.node_tree, LAYER_FRAME_ROLE)
    rectangles = []
    for frame in frames:
        layer_uuid = str(frame.get(LAYER_UUID_KEY, ""))
        children = tuple(
            node
            for node in material.node_tree.nodes
            if _pointer(node.parent) == _pointer(frame)
        )
        if not children:
            continue
        left = min(float(node.location_absolute.x) for node in children) - 60.0
        right = max(
            float(node.location_absolute.x) + max(float(node.width), 1.0)
            for node in children
        ) + 60.0
        top = max(float(node.location_absolute.y) for node in children) + 60.0
        bottom = min(
            float(node.location_absolute.y)
            - (
                float(node.dimensions.y)
                if float(node.dimensions.y) > 1.0
                else 180.0
            )
            for node in children
        ) - 60.0
        rectangles.append((layer_uuid, left, bottom, right, top))
    return tuple(rectangles)


__all__ = [
    "ADJUSTMENT_APPLY_MIX_NODE_ROLE",
    "ADJUSTMENT_MASK_BW_NODE_ROLE",
    "ADJUSTMENT_MASK_IMAGE_NODE_ROLE",
    "ADJUSTMENT_NODE_ROLE",
    "ADJUSTMENT_UUID_KEY",
    "COMPOSITE_LAYER_ROLE",
    "LAYER_FRAME_ROLE",
    "LAYER_IMAGE_NODE_ROLE",
    "LAYER_IMAGE_ROLE",
    "LAYER_MIX_NODE_ROLE",
    "LAYER_SOLO_PREVIEW_MIX_NODE_ROLE",
    "LAYER_PREVIEW_UUID_KEY",
    "LAYER_UUID_KEY",
    "LayerShaderError",
    "MASK_BW_NODE_ROLE",
    "MASK_IMAGE_NODE_ROLE",
    "MASK_IMAGE_ROLE",
    "MASK_PREVIEW_UUID_KEY",
    "MASK_UUID_KEY",
    "ORIGINAL_GATE_NODE_ROLE",
    "ORIGINAL_PREVIEW_KEY",
    "active_base_color_graph_layers",
    "add_layer_transaction",
    "composite_chain_layers",
    "composite_layer_is_editable",
    "composite_session_is_active",
    "composite_state_locks_layer_edits",
    "composite_source_layers",
    "create_composite_layer_image",
    "detach_shared_owned_image",
    "remove_layer_resources",
    "remove_owned_layer_bundles",
    "restore_original_layer",
    "connect_new_layer_nodes",
    "find_adjustment",
    "find_mask",
    "find_layer",
    "has_layer_backend",
    "layout_layer_nodes",
    "managed_node_rectangles",
    "original_layer",
    "original_layer_is_read_only",
    "reconcile_layer_graph",
    "remove_adjustment_resources",
    "remove_mask_resources",
    "rename_layer_frame",
    "activate_layer_paint_image",
    "activate_mask_paint_image",
    "sync_adjustment_node",
    "toggle_layer_preview",
    "toggle_mask_preview",
    "update_adjustment",
    "update_mask",
    "update_layer_visibility",
    "validate_active_layer_graph",
    "validate_layer_graph",
]
