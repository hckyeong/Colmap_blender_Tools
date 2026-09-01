"""Paint Layer Stack을 원본 Texture 또는 빈 Layer로 안전하게 재초기화합니다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bpy

from .layer_shader import (
    LAYER_IMAGE_NODE_ROLE,
    LAYER_IMAGE_ROLE,
    MASK_IMAGE_ROLE,
    NORMAL_LAYER_ROLE,
    ORIGINAL_LAYER_IMAGE_KEY,
    LayerShaderError,
    _activate_paint_image_node,
    _create_original_image,
    _external_original_image,
    _image_size,
    _image_tile_numbers,
    _new_image,
    _node_for,
    _owned_image,
    _pointer,
    _remove_image,
    _validate_target_image_contract,
    _target_tile_numbers,
    original_layer_is_read_only,
    reconcile_layer_graph,
    validate_active_layer_graph,
)
from .ownership import new_uuid
from .udim_setup import UDIM_KIND_KEY


RESET_PENDING_DELETE_KEY = "pp_layer_reset_pending_delete"


def _snapshot_mask(mask: Any) -> dict[str, Any]:
    """Mask PropertyGroup을 RNA 참조 없이 복원 가능한 값으로 복사합니다."""
    return {
        "mask_uuid": str(mask.mask_uuid or ""),
        "ui_selected": bool(mask.ui_selected),
        "image": mask.image,
        "enabled": bool(mask.enabled),
    }


def _snapshot_adjustment(adjustment: Any) -> dict[str, Any]:
    """Adjustment와 자체 Mask를 값 Snapshot으로 복사합니다."""
    return {
        "adjustment_type": str(adjustment.adjustment_type),
        "adjustment_uuid": str(adjustment.adjustment_uuid or ""),
        "ui_selected": bool(adjustment.ui_selected),
        "image": adjustment.image,
        "mask": _snapshot_mask(adjustment.mask),
        "expanded": bool(adjustment.expanded),
        "enabled": bool(adjustment.enabled),
        "brightness": float(adjustment.brightness),
        "exposure": float(adjustment.exposure),
        "hue": float(adjustment.hue),
        "saturation": float(adjustment.saturation),
    }


def _snapshot_layer(layer: Any) -> dict[str, Any]:
    """Layer와 모든 자식을 UUID·값·Image Pointer Snapshot으로 복사합니다."""
    return {
        "name": str(layer.name),
        "layer_uuid": str(layer.layer_uuid),
        "layer_role": str(layer.layer_role),
        "composite_session_uuid": str(layer.composite_session_uuid or ""),
        "composite_generation": int(layer.composite_generation),
        "composite_source_session_uuid": str(
            layer.composite_source_session_uuid or ""
        ),
        "composite_bake_included": bool(layer.composite_bake_included),
        "composite_saved_visible": bool(layer.composite_saved_visible),
        "composite_source_expanded": bool(layer.composite_source_expanded),
        "ui_selected": bool(layer.ui_selected),
        "is_original_layer": bool(layer.is_original_layer),
        "image": layer.image,
        "visible": bool(layer.visible),
        "expanded": bool(layer.expanded),
        "mask": _snapshot_mask(layer.mask),
        "adjustments": tuple(
            _snapshot_adjustment(adjustment) for adjustment in layer.adjustments
        ),
        "active_adjustment_index": int(layer.active_adjustment_index),
    }


def _assign_mask(mask: Any, snapshot: dict[str, Any]) -> None:
    """Mask Snapshot을 Update Callback 없이 PropertyGroup에 복원합니다."""
    mask.mask_uuid = snapshot["mask_uuid"]
    mask.ui_selected = bool(snapshot.get("ui_selected", False))
    mask.image = snapshot["image"]
    mask["enabled"] = snapshot["enabled"]


def _assign_adjustment(adjustment: Any, snapshot: dict[str, Any]) -> None:
    """Adjustment Snapshot과 자체 Mask를 PropertyGroup에 복원합니다."""
    adjustment.adjustment_type = snapshot["adjustment_type"]
    adjustment.adjustment_uuid = snapshot["adjustment_uuid"]
    adjustment.ui_selected = bool(snapshot.get("ui_selected", False))
    adjustment.image = snapshot["image"]
    adjustment["expanded"] = snapshot["expanded"]
    adjustment["enabled"] = snapshot["enabled"]
    adjustment["brightness"] = snapshot["brightness"]
    adjustment["exposure"] = snapshot["exposure"]
    adjustment["hue"] = snapshot["hue"]
    adjustment["saturation"] = snapshot["saturation"]
    _assign_mask(adjustment.mask, snapshot["mask"])


def _assign_layer(layer: Any, snapshot: dict[str, Any]) -> None:
    """Layer Snapshot을 새 Collection 항목에 완전하게 복원합니다."""
    for name in (
        "name",
        "layer_uuid",
        "layer_role",
        "composite_session_uuid",
        "composite_generation",
        "composite_source_session_uuid",
        "composite_bake_included",
        "composite_saved_visible",
        "composite_source_expanded",
        "ui_selected",
        "is_original_layer",
        "expanded",
        "active_adjustment_index",
    ):
        setattr(layer, name, snapshot[name])
    layer.image = snapshot["image"]
    layer["visible"] = snapshot["visible"]
    _assign_mask(layer.mask, snapshot["mask"])
    layer.adjustments.clear()
    for adjustment_snapshot in snapshot["adjustments"]:
        _assign_adjustment(layer.adjustments.add(), adjustment_snapshot)


def _replace_layer_collection(settings: Any, snapshots: tuple[dict[str, Any], ...]) -> None:
    """CollectionProperty를 비운 뒤 값 Snapshot으로만 다시 구성합니다."""
    settings.layers.clear()
    for snapshot in snapshots:
        _assign_layer(settings.layers.add(), snapshot)


def _settings_snapshot(settings: Any) -> dict[str, Any]:
    """초기화가 바꾸는 Scene 선택·Preview·Image Pointer 상태를 복사합니다."""
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
        "durability_state": str(settings.durability_state),
        "original_layer_name": str(settings.original_layer_name or "Original Layer"),
        "layer_selection_anchor_uuid": str(
            settings.layer_selection_anchor_uuid or ""
        ),
    }


def _restore_settings(settings: Any, snapshot: dict[str, Any]) -> None:
    """Scene 선택·Preview·Image Pointer를 Transaction 이전 값으로 복원합니다."""
    for name, value in snapshot.items():
        setattr(settings, name, value)


def _snapshot_images(layer_snapshots: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    """Layer Snapshot이 소유한 Layer·Mask Image를 중복 없이 반환합니다."""
    images = []
    pointers = set()
    for layer in layer_snapshots:
        candidates = [layer["image"], layer["mask"]["image"]]
        candidates.extend(item["mask"]["image"] for item in layer["adjustments"])
        for image in candidates:
            pointer = _pointer(image)
            if pointer and pointer not in pointers:
                pointers.add(pointer)
                images.append(image)
    return tuple(images)


def _set_reset_protection(images: tuple[Any, ...], enabled: bool) -> None:
    """Graph 재구성 중 기존 관리 Image가 고아 정리되지 않게 보호합니다."""
    for image in images:
        if not _owned_image(image, {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE}):
            continue
        if enabled:
            image[RESET_PENDING_DELETE_KEY] = True
        elif RESET_PENDING_DELETE_KEY in image:
            del image[RESET_PENDING_DELETE_KEY]


def _source_files_exist(image: Any, tile_numbers: tuple[int, ...]) -> bool:
    """Pack되지 않은 Original Source의 모든 파일 타일이 실제로 존재하는지 확인합니다."""
    raw_path = str(getattr(image, "filepath_raw", "") or getattr(image, "filepath", ""))
    if not raw_path:
        return False
    absolute = str(bpy.path.abspath(raw_path))
    paths = []
    for number in tile_numbers:
        u = (int(number) - 1001) % 10 + 1
        v = (int(number) - 1001) // 10 + 1
        resolved = absolute.replace("<UDIM>", str(number)).replace(
            "<UVTILE>", f"u{u}_v{v}"
        )
        paths.append(Path(resolved))
    return all(path.is_file() for path in paths)


def _validated_original_source(
    settings: Any,
    target: Any,
    material: Any,
) -> tuple[Any | None, Any | None]:
    """최초 UDIM kind와 보존 provenance로 Texture 또는 UV-only 분기를 확정합니다."""
    kind = str(target.get(UDIM_KIND_KEY, "") or "")
    if kind == "UV_ONLY":
        return None, None
    if kind not in {"SINGLE_1001", "EXISTING_UDIM", "MULTI_MATERIAL_TILES"}:
        raise LayerShaderError("최초 Original Source 종류 기록이 유효하지 않습니다.")
    source = getattr(settings, "original_source_image", None)
    if source is None or _pointer(source) == 0:
        raise LayerShaderError(
            "Texture Original Source 기록이 유실되어 기존 Layer Stack을 유지합니다."
        )
    tiles = _target_tile_numbers(target)
    if _image_tile_numbers(source) != tiles:
        raise LayerShaderError("Original Source의 UDIM 타일 집합이 관리 Target과 다릅니다.")
    if str(getattr(source, "source", "")) == "GENERATED":
        return source, None
    packed_count = len(tuple(getattr(source, "packed_files", ())))
    has_packed_data = bool(getattr(source, "packed_file", None)) or packed_count > 0
    if not has_packed_data and not _source_files_exist(source, tiles):
        raise LayerShaderError(
            "Original Source 파일과 Packed 데이터가 없어 기존 Layer Stack을 유지합니다."
        )
    return source, None


def _prepare_initial_image(
    settings: Any,
    target: Any,
    layer_uuid: str,
    size: tuple[int, int],
    reference: Any,
    source: Any | None,
    source_socket: Any | None,
) -> tuple[Any, bool]:
    """검증된 provenance로 외부 Original 참조 또는 관리 Paint Image를 준비합니다."""
    if source is None and source_socket is None:
        image = _new_image(
            target,
            f"{target.name}_Layer 1_Paint",
            size,
            role=LAYER_IMAGE_ROLE,
            layer_uuid=layer_uuid,
            color=(0.0, 0.0, 0.0, 0.0),
            reference=reference,
        )
        return image, False
    image = _create_original_image(
        target,
        layer_uuid,
        f"{target.name}_Original Layer · Texture_Paint",
        size,
        source,
        tuple(float(value) for value in settings.original_source_color),
        reference,
        scene=settings.id_data,
        material=settings.paint_material,
        source_socket=source_socket,
    )
    if _owned_image(image, {LAYER_IMAGE_ROLE}):
        image[ORIGINAL_LAYER_IMAGE_KEY] = True
    return image, True


def _initial_layer_snapshot(
    previous: dict[str, Any],
    image: Any,
    is_original: bool,
) -> dict[str, Any]:
    """기존 생존 UUID를 유지한 초기 Layer 한 개의 값 Snapshot을 만듭니다."""
    return {
        **previous,
        "name": "Original Layer · Texture" if is_original else "Layer 1",
        "layer_role": NORMAL_LAYER_ROLE,
        "composite_session_uuid": "",
        "composite_generation": 0,
        "composite_source_session_uuid": "",
        "composite_bake_included": False,
        "composite_saved_visible": True,
        "composite_source_expanded": True,
        "ui_selected": True,
        "is_original_layer": is_original,
        "image": image,
        "visible": True,
        "expanded": True,
        "mask": {
            "mask_uuid": "",
            "ui_selected": False,
            "image": None,
            "enabled": True,
        },
        "adjustments": (),
        "active_adjustment_index": -1,
    }


def replace_with_fresh_layer(
    context: Any,
    width: int,
    height: int,
    *,
    use_base_resolution: bool = True,
) -> Any:
    """해상도 변경 시 쓰기 Layer와 필요하면 읽기 전용 Original을 원자 초기화합니다."""
    scene = context.scene
    settings = scene.projection_paint
    if str(getattr(settings, "composite_state", "NONE")) != "NONE":
        raise LayerShaderError("Composite를 먼저 해제한 뒤 해상도를 변경하십시오.")
    if len(settings.layers) == 0 or settings.paint_material is None:
        raise LayerShaderError("초기화할 Paint Layer가 없습니다.")
    target = settings.paint_target
    material = settings.paint_material
    first_snapshot = _snapshot_layer(settings.layers[0])
    layer_uuid = str(first_snapshot["layer_uuid"])
    image_node = _node_for(material.node_tree, LAYER_IMAGE_NODE_ROLE, layer_uuid)
    if image_node is None or not _pointer(first_snapshot["image"]):
        raise LayerShaderError("초기화할 Layer Image Texture Node를 찾을 수 없습니다.")
    reference_size = _image_size(first_snapshot["image"])
    for layer in settings.layers:
        for image in (
            layer.image,
            layer.mask.image,
            *(adjustment.mask.image for adjustment in layer.adjustments),
        ):
            if not _pointer(image):
                continue
            if image is layer.image and original_layer_is_read_only(settings, layer):
                _external_original_image(image, require_files=True)
            else:
                _validate_target_image_contract(
                    target,
                    image,
                    reference_size,
                    first_snapshot["image"],
                )
    source, source_socket = _validated_original_source(settings, target, material)
    size = (int(width), int(height))
    if min(size) <= 0:
        raise LayerShaderError("새 Layer 해상도는 0보다 커야 합니다.")
    if use_base_resolution:
        source_image = source
        if source_image is None and source_socket is not None:
            source_image = getattr(getattr(source_socket, "node", None), "image", None)
        if source_image is not None and _pointer(source_image):
            size = _image_size(source_image)

    prepared_image, is_original = _prepare_initial_image(
        settings,
        target,
        layer_uuid,
        size,
        first_snapshot["image"],
        source,
        source_socket,
    )
    direct_original = bool(
        is_original
        and source is not None
        and _external_original_image(source, require_files=True) is prepared_image
    )
    if direct_original:
        new_image = _new_image(
            target,
            f"{target.name}_Layer 1_Paint",
            size,
            role=LAYER_IMAGE_ROLE,
            layer_uuid=layer_uuid,
            color=(0.0, 0.0, 0.0, 0.0),
            reference=first_snapshot["image"],
        )
    else:
        new_image = prepared_image
    try:
        _validate_target_image_contract(target, new_image, size, first_snapshot["image"])
    except Exception:
        _remove_image(new_image)
        raise

    layer_snapshots = tuple(_snapshot_layer(layer) for layer in settings.layers)
    scene_snapshot = _settings_snapshot(settings)
    old_images = _snapshot_images(layer_snapshots)
    initial_snapshot = _initial_layer_snapshot(
        first_snapshot,
        new_image,
        is_original and not direct_original,
    )
    initial_snapshots = (initial_snapshot,)
    if direct_original:
        original_seed = {**first_snapshot, "layer_uuid": new_uuid()}
        original_snapshot = _initial_layer_snapshot(
            original_seed,
            prepared_image,
            True,
        )
        original_snapshot["ui_selected"] = False
        initial_snapshots = (initial_snapshot, original_snapshot)
    _set_reset_protection(old_images, True)
    try:
        _replace_layer_collection(settings, initial_snapshots)
        settings.active_layer_index = 0
        settings.active_mask_uuid = ""
        settings.paint_mask_mode = False
        settings.layer_preview_uuid = ""
        settings.mask_preview_adjustment_uuid = ""
        settings.layer_selection_anchor_uuid = layer_uuid
        settings.overlay_image = new_image
        settings.active_paint_image = new_image
        settings.original_layer_name = (
            str(initial_snapshots[-1]["name"])
            if direct_original
            else str(initial_snapshot["name"])
        )
        settings.durability_state = "EMPTY"
        reconcile_layer_graph(scene)
        _activate_paint_image_node(material, LAYER_IMAGE_NODE_ROLE, new_image)
        validate_active_layer_graph(scene, material)
    except Exception as error:
        rollback_error = None
        try:
            _replace_layer_collection(settings, layer_snapshots)
            _restore_settings(settings, scene_snapshot)
            reconcile_layer_graph(scene)
            validate_active_layer_graph(scene, material)
        except Exception as exc:
            rollback_error = exc
        finally:
            _set_reset_protection(old_images, False)
            if _pointer(new_image) and int(getattr(new_image, "users", 0)) == 0:
                _remove_image(new_image)
        if rollback_error is not None:
            raise LayerShaderError(
                f"해상도 초기화 실패 뒤 기존 Layer Stack 복구에도 실패했습니다: {rollback_error}"
            ) from error
        raise

    _set_reset_protection(old_images, False)
    for image in old_images:
        if (
            _owned_image(image, {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE})
            and image is not new_image
            and int(image.users) == 0
        ):
            _remove_image(image)
    return new_image


__all__ = ["replace_with_fresh_layer"]
