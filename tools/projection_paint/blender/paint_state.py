"""Blender ID와 Scene Pointer에서 Paint·Stencil 권위 상태를 계산합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import bpy

from .material_setup import (
    MATERIAL_ROLE,
    OVERLAY_ROLE,
    PaintMaterialSetup,
    PaintSetupError,
    setup_from_scene_state,
)
from .ownership import (
    DATASET_UUID_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    new_uuid,
)


STATE_SCHEMA_VERSION = 2
STENCIL_TEXTURE_ROLE = "STENCIL_TEXTURE"
PAINT_MESH_UUID_KEY = "pp_paint_mesh_uuid"


class PaintStateError(RuntimeError):
    """Blender 권위 Paint 상태를 하나로 확정할 수 없음을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class PaintStateSnapshot:
    """한 호출 동안만 사용하는 Blender 권위 Paint 상태 Snapshot입니다."""

    scene: Any
    target: Any | None
    setup: PaintMaterialSetup | None
    overlay: Any | None
    texture: Any | None
    brush: Any | None
    source_image: Any | None
    image_id: int
    state: str
    reason: str = ""


def _pointer(value: Any) -> int:
    """삭제되었거나 무효한 Blender RNA Pointer를 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _owned(value: Any, role: str) -> bool:
    """Blender ID가 요청한 애드온 소유 역할인지 확인합니다."""
    try:
        return bool(
            _pointer(value)
            and value.get(OWNER_KEY) == OWNER_TOKEN
            and value.get(RECORD_KIND_KEY) == role
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def resolve_overlay(scene: Any):
    """Scene Pointer와 Material Node가 함께 가리키는 권위 Overlay를 반환합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return None
    overlay = settings.overlay_image
    if not _owned(overlay, OVERLAY_ROLE):
        return None
    material = settings.paint_material
    if not _owned(material, MATERIAL_ROLE) or material.node_tree is None:
        return None
    matches = tuple(
        node
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE"
        and node.get(OWNER_KEY) == OWNER_TOKEN
        and node.get(RECORD_KIND_KEY) == "OVERLAY_IMAGE_NODE"
        and node.image is overlay
    )
    return overlay if len(matches) == 1 else None


def resolve_stencil_texture(scene: Any):
    """Scene Pointer가 가리키는 단일 관리 Stencil Texture를 반환합니다."""
    settings = getattr(scene, "projection_paint", None)
    texture = getattr(settings, "stencil_texture", None) if settings is not None else None
    return texture if _owned(texture, STENCIL_TEXTURE_ROLE) else None


def resolve_active_brush(scene: Any):
    """현재 Texture Paint Brush 또는 저장된 유효 Brush Pointer를 반환합니다."""
    settings = getattr(scene, "projection_paint", None)
    image_paint = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    active = getattr(image_paint, "brush", None)
    if _pointer(active):
        return active
    stored = getattr(settings, "stencil_brush", None) if settings is not None else None
    return stored if _pointer(stored) else None


def validate_paint_contract(scene: Any) -> PaintMaterialSetup:
    """Scene Pointer가 가리키는 Target·UV·Material·Overlay 계약을 검증합니다."""
    try:
        return setup_from_scene_state(scene)
    except PaintSetupError as exc:
        raise PaintStateError(str(exc)) from exc


def derive_paint_state(scene: Any) -> tuple[str, str]:
    """실제 Blender 연결에서 UI용 Paint 상태와 원인을 계산합니다."""
    settings = getattr(scene, "projection_paint", None)
    target = getattr(settings, "paint_target", None) if settings is not None else None
    material = getattr(settings, "paint_material", None) if settings is not None else None
    if settings is None or not _pointer(target) or not _pointer(material):
        return "IDLE", ""
    try:
        setup = validate_paint_contract(scene)
    except PaintStateError as exc:
        return "STALE", str(exc)
    if setup.target.mode != "TEXTURE_PAINT":
        return "READY", "Paint Target이 Texture Paint Mode가 아닙니다."
    if bool(settings.paint_mask_mode) and _pointer(settings.active_paint_image):
        return "ACTIVE", ""
    texture = resolve_stencil_texture(scene)
    source = settings.stencil_source_image
    brush = resolve_active_brush(scene)
    if texture is None or not _pointer(source):
        return "STALE", "현재 Stencil Texture 또는 Source Image가 없습니다."
    if bool(settings.manual_stencil_disabled):
        return "MANUAL_DISABLED", ""
    if bool(settings.stencil_outside_camera_view):
        return "FROZEN", ""
    if brush is None:
        return "STALE", "활성 Texture Paint Brush가 없습니다."
    if brush.texture is not texture or brush.texture_slot.map_mode != "STENCIL":
        return "MANUAL_DISABLED", ""
    return "ACTIVE", ""


def resolve_paint_state(scene: Any) -> PaintStateSnapshot:
    """현재 Blender ID를 짧은 Paint 상태 Snapshot으로 묶어 반환합니다."""
    settings = getattr(scene, "projection_paint", None)
    state, reason = derive_paint_state(scene)
    setup = None
    target = getattr(settings, "paint_target", None) if settings is not None else None
    if settings is not None and _pointer(target) and target.mode == "TEXTURE_PAINT":
        try:
            setup = validate_paint_contract(scene)
        except PaintStateError:
            setup = None
    return PaintStateSnapshot(
        scene=scene,
        target=getattr(settings, "paint_target", None) if settings is not None else None,
        setup=setup,
        overlay=resolve_overlay(scene),
        texture=resolve_stencil_texture(scene),
        brush=resolve_active_brush(scene),
        source_image=(
            getattr(settings, "stencil_source_image", None)
            if settings is not None
            else None
        ),
        image_id=int(getattr(settings, "active_image_id", 0) or 0),
        state=state,
        reason=reason,
    )


def commit_paint_setup(scene: Any, setup: PaintMaterialSetup) -> None:
    """성공한 Material Transaction을 Scene Pointer 권위 상태에 기록합니다."""
    settings = scene.projection_paint
    preserve_active_mask = bool(
        settings.paint_target is setup.target
        and settings.paint_material is setup.material
        and bool(settings.paint_mask_mode)
        and _pointer(settings.active_paint_image)
    )
    previous_active_image = settings.active_paint_image
    settings.paint_target = setup.target
    mesh_uuid = str(setup.mesh.get(PAINT_MESH_UUID_KEY, "") or "")
    if not mesh_uuid:
        mesh_uuid = new_uuid()
        setup.mesh[PAINT_MESH_UUID_KEY] = mesh_uuid
    settings.paint_mesh_uuid = mesh_uuid
    settings.paint_material = setup.material
    settings.overlay_image = setup.overlay
    settings.active_paint_image = (
        previous_active_image if preserve_active_mask else setup.overlay
    )
    settings.paint_mask_mode = preserve_active_mask
    settings.paint_uv_name = setup.uv_name
    settings.paint_material_slot_index = setup.slot_index
    settings.paint_material_slot_link = setup.slot_link
    settings.original_layer_name = "Original Layer"
    settings.state_schema_version = STATE_SCHEMA_VERSION


def _managed_material_overlay(material: Any):
    """구버전 관리 Material에서 단일 Overlay Image를 변경 없이 찾습니다."""
    if not _owned(material, MATERIAL_ROLE) or material.node_tree is None:
        return None
    images = tuple(
        node.image
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE"
        and node.get(OWNER_KEY) == OWNER_TOKEN
        and node.get(RECORD_KIND_KEY) == "OVERLAY_IMAGE_NODE"
        and _owned(node.image, OVERLAY_ROLE)
    )
    return images[0] if len(images) == 1 else None


def migrate_paint_state(scene: Any) -> bool:
    """구버전 Blender 데이터를 안전한 Scene Pointer 스키마로 한 번 이전합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None or int(settings.state_schema_version) >= STATE_SCHEMA_VERSION:
        return False
    target = settings.paint_target
    material = getattr(target, "active_material", None) if _pointer(target) else None
    if _owned(material, MATERIAL_ROLE):
        settings.paint_material = material
        mesh_uuid = str(target.data.get(PAINT_MESH_UUID_KEY, "") or "")
        if not mesh_uuid:
            mesh_uuid = new_uuid()
            target.data[PAINT_MESH_UUID_KEY] = mesh_uuid
        settings.paint_mesh_uuid = mesh_uuid
        uv = getattr(getattr(target.data, "uv_layers", None), "active", None)
        settings.paint_uv_name = str(getattr(uv, "name", "") or "")
        settings.paint_material_slot_index = int(target.active_material_index)
        if len(target.material_slots):
            settings.paint_material_slot_link = str(
                target.material_slots[int(target.active_material_index)].link
            )
        overlay = _managed_material_overlay(material)
        if overlay is not None:
            settings.overlay_image = overlay
            settings.active_paint_image = overlay
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    textures = tuple(
        texture
        for texture in bpy.data.textures
        if _owned(texture, STENCIL_TEXTURE_ROLE)
        and (
            not dataset_uuid
            or str(texture.get(DATASET_UUID_KEY, "") or "") == dataset_uuid
        )
    )
    if len(textures) == 1:
        settings.stencil_texture = textures[0]
        if _pointer(textures[0].image):
            settings.stencil_source_image = textures[0].image
    active_brush = resolve_active_brush(scene)
    if (
        active_brush is not None
        and settings.stencil_texture is not None
        and active_brush.texture is settings.stencil_texture
    ):
        settings.stencil_brush = active_brush
        if _owned(active_brush, "STENCIL_BRUSH"):
            settings.stencil_owns_brush = True
            settings.original_brush_asset_library_type = "ESSENTIALS"
            settings.original_brush_asset_library_identifier = ""
            settings.original_brush_asset_relative_identifier = (
                "brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard"
            )
    try:
        valid_actual_paint = bool(
            _pointer(target)
            and target.mode == "TEXTURE_PAINT"
            and validate_paint_contract(scene)
        )
    except PaintStateError:
        valid_actual_paint = False
    settings.paint_enabled = valid_actual_paint
    try:
        settings.active_image_id = max(0, int(str(settings.current_image_id or "0")))
    except ValueError:
        settings.active_image_id = 0
    settings.state_schema_version = STATE_SCHEMA_VERSION
    if (
        _pointer(target)
        and settings.paint_material is not None
        and settings.overlay_image is not None
    ):
        try:
            from .layer_shader import reconcile_layer_graph

            reconcile_layer_graph(
                scene,
                material=settings.paint_material,
                overlay=settings.overlay_image,
            )
        except RuntimeError:
            settings.paint_enabled = False
    return True


def reconcile_paint_state(context: Any, reason: str = "") -> PaintStateSnapshot:
    """Undo·Redo·Load 뒤 Blender Pointer를 복구하고 실제 상태로 UI를 동기화합니다."""
    scene = getattr(context, "scene", None)
    if scene is None:
        raise PaintStateError("Paint 상태를 복구할 Scene이 없습니다.")
    migrate_paint_state(scene)
    settings = scene.projection_paint
    if settings.paint_material is not None and settings.overlay_image is not None:
        try:
            from .layer_shader import reconcile_layer_graph

            reconcile_layer_graph(scene)
        except RuntimeError as exc:
            settings.paint_state = "ERROR"
            settings.status = f"Layer Graph 복구 실패: {exc}"
            settings.status_kind = "ERROR"
    snapshot = resolve_paint_state(scene)
    settings.paint_state = snapshot.state if snapshot.state != "READY" else "IDLE"
    if snapshot.reason:
        settings.status = snapshot.reason
        settings.status_kind = "ERROR" if snapshot.state == "STALE" else "WARNING"
    elif reason and snapshot.state in {"ACTIVE", "FROZEN", "MANUAL_DISABLED"}:
        settings.status = f"Paint 상태 복구 완료 ({reason})"
        settings.status_kind = "SUCCESS"
    return snapshot


__all__ = [
    "PaintStateError",
    "PaintStateSnapshot",
    "STATE_SCHEMA_VERSION",
    "commit_paint_setup",
    "derive_paint_state",
    "migrate_paint_state",
    "reconcile_paint_state",
    "resolve_active_brush",
    "resolve_overlay",
    "resolve_paint_state",
    "resolve_stencil_texture",
    "validate_paint_contract",
]
