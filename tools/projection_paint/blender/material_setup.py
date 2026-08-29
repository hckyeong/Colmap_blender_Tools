"""Paint Target을 단일 UDIM 관리 Material과 Layer Overlay로 준비합니다."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import uuid

import bpy

from .image_backend import (
    IMAGE_BACKEND_KEY,
    PaintImageContract,
    PaintImageContractError,
    backend_from_layout,
    backend_from_target,
    image_contract,
    new_managed_paint_image,
    validate_image_contract,
)
from .ownership import (
    CURRENT_SCHEMA,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    SCHEMA_KEY,
    UUID_KEY,
)
from .udim_setup import (
    ORIGINAL_ACTIVE_SLOT_KEY,
    ORIGINAL_FACE_ATTRIBUTE,
    PAINT_UV_NAME,
    STAGING_DIRECTORY_KEY,
    UDIM_KIND_KEY,
    UDIM_MANAGED_SLOT_KEY,
    UDIM_SIGNATURE_KEY,
    UDIM_SOURCE_UV_KEY,
    UDIM_TILES_KEY,
    UDIM_UV_KEY,
    UDIMSetupError,
    analyze_udim_source,
    build_paint_uv,
    layout_from_target,
    paint_image_size,
    prepare_udim_source_image,
    remove_paint_staging_path,
    write_original_face_indices,
)

ROLE_KEY = RECORD_KIND_KEY
TARGET_KEY = "pp_target_uuid"
MATERIAL_ROLE = "PAINT_MATERIAL"
OVERLAY_ROLE = "OVERLAY_PERSISTENT"
OVERLAY_NODE_ROLE = "OVERLAY_IMAGE_NODE"
MIX_NODE_ROLE = "OVERLAY_MIX_NODE"
ORIGINAL_GATE_NODE_ROLE = "ORIGINAL_BASE_GATE"
SCHEMA_VERSION = CURRENT_SCHEMA
PAINT_MESH_UUID_KEY = "pp_paint_mesh_uuid"
_MANAGED_UDIM_METADATA_KEYS = (
    UDIM_KIND_KEY,
    UDIM_TILES_KEY,
    UDIM_SIGNATURE_KEY,
    UDIM_UV_KEY,
    UDIM_SOURCE_UV_KEY,
    UDIM_MANAGED_SLOT_KEY,
    ORIGINAL_ACTIVE_SLOT_KEY,
)


class PaintSetupError(RuntimeError):
    """Paint 준비 계약 위반을 나타냅니다."""


MANAGED_OVERLAY_VALID = "VALID"
MANAGED_OVERLAY_LEGACY_VALID = "LEGACY_VALID"
MANAGED_OVERLAY_RECOVERABLE_FILE_1001 = "RECOVERABLE_FILE_1001"
MANAGED_OVERLAY_INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ManagedOverlayClassification:
    """관리 Overlay의 읽기 전용 형식·타일 복구 가능 상태입니다."""

    state: str
    reason: str
    expected_tiles: tuple[int, ...] = ()
    actual_tiles: tuple[int, ...] = ()
    source: str = ""


@dataclass(frozen=True, slots=True)
class PaintMaterialProgress:
    """UDIM Material 준비 단계와 완료 작업량을 불변 값으로 보관합니다."""

    phase: str
    completed_units: int
    total_units: int
    detail: str

    @property
    def fraction(self) -> float:
        """완료 작업량을 0..1 범위의 진행률로 반환합니다."""

        if self.total_units <= 0:
            return 0.0
        return max(
            0.0,
            min(float(self.completed_units) / float(self.total_units), 1.0),
        )


class PaintMaterialPreparationJob:
    """단일 UDIM Material 준비 Generator를 단계별로 실행합니다."""

    def __init__(
        self,
        target,
        width: int,
        height: int,
        *,
        use_base_resolution: bool,
        replace_existing_overlay: bool,
    ) -> None:
        """고정된 입력으로 지연 실행할 Material 준비 작업을 만듭니다."""

        self._steps = _prepare_active_material_steps(
            target,
            int(width),
            int(height),
            use_base_resolution=bool(use_base_resolution),
            replace_existing_overlay=bool(replace_existing_overlay),
        )
        self.progress = PaintMaterialProgress(
            "Preparing paint image",
            0,
            11,
            f"대상: {getattr(target, 'name', 'Unknown')}",
        )
        self.result: PaintMaterialSetup | None = None
        self.done = False

    def advance(self) -> PaintMaterialProgress:
        """다음 실제 준비 단계 하나를 실행하고 최신 진행값을 반환합니다."""

        if self.done:
            return self.progress
        try:
            self.progress = next(self._steps)
        except StopIteration as stop:
            self.result = stop.value
            self.done = True
            if self.result is None:
                raise PaintSetupError("UDIM Material 준비 결과가 없습니다.")
            self.progress = PaintMaterialProgress(
                "Texture Paint ready",
                11,
                11,
                f"대상: {getattr(self.result.target, 'name', 'Unknown')}",
            )
        return self.progress

    def abort(self) -> None:
        """완료 전 Generator를 닫아 생성 중인 관리 자원을 Rollback합니다."""

        if self.done:
            return
        self._steps.close()
        self.done = True


def _pointer(value) -> int:
    """삭제된 Blender ID를 안전하게 0으로 변환합니다."""
    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _remove_unused_paint_image(image, *, allow_dirty: bool) -> None:
    """참조가 없는 관리 Paint Image와 소유 Staging을 함께 제거합니다."""
    if image is None or _pointer(image) == 0 or int(image.users) != 0:
        return
    if bool(image.is_dirty) and not allow_dirty:
        return
    staging_path = str(image.get(STAGING_DIRECTORY_KEY, "") or "")
    bpy.data.images.remove(image)
    if staging_path:
        remove_paint_staging_path(staging_path)


def _object_uuid(target) -> str:
    """Paint Target의 안정적인 소유권 UUID를 반환합니다."""
    value = str(target.get(TARGET_KEY, ""))
    if value:
        return value
    value = uuid.uuid4().hex
    target[TARGET_KEY] = value
    return value


def _active_uv(target):
    """Paint Target의 활성 UV Layer를 반환합니다."""
    layers = getattr(getattr(target, "data", None), "uv_layers", None)
    return getattr(layers, "active", None) if layers is not None else None


def validate_paint_target(target) -> None:
    """단일 UDIM 관리 재질로 안전하게 전환할 Mesh인지 검증합니다."""
    if target is None or _pointer(target) == 0 or target.type != "MESH":
        raise PaintSetupError("Paint Target은 유효한 Mesh Object여야 합니다.")
    mesh = getattr(target, "data", None)
    if mesh is None or _pointer(mesh) == 0 or len(mesh.polygons) == 0:
        raise PaintSetupError("Paint Target Mesh에 칠할 면이 없습니다.")
    if target.library is not None or mesh.library is not None:
        raise PaintSetupError("Library Link Mesh는 로컬 복사 후 사용하십시오.")
    if _active_uv(target) is None:
        raise PaintSetupError("UV Map이 없어 Texture Paint를 시작할 수 없습니다.")
    if int(mesh.users) > 1:
        raise PaintSetupError(
            "공유 Mesh는 UV와 폴리곤 재질 인덱스를 변경하므로 Single User로 만드십시오."
        )
    if len(target.material_slots):
        index = int(target.active_material_index)
        if index < 0 or index >= len(target.material_slots):
            raise PaintSetupError("활성 Material Slot이 유효하지 않습니다.")


def _active_output(material):
    """활성 Material Output Node를 유일하게 찾습니다."""
    outputs = tuple(
        node
        for node in material.node_tree.nodes
        if node.type == "OUTPUT_MATERIAL" and bool(node.is_active_output)
    )
    if len(outputs) != 1:
        raise PaintSetupError("활성 Material Output이 정확히 하나여야 합니다.")
    return outputs[0]


def _supported_principled(material):
    """지원하는 활성 Principled BSDF를 반환합니다."""
    output = _active_output(material)
    surface = output.inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        raise PaintSetupError("Material Output Surface가 Principled BSDF에 연결되어야 합니다.")
    node = surface.links[0].from_node
    if node.type != "BSDF_PRINCIPLED":
        raise PaintSetupError("1차 버전은 활성 Principled BSDF Material만 지원합니다.")
    if node.inputs.get("Base Color") is None:
        raise PaintSetupError("Principled BSDF Base Color 입력을 찾지 못했습니다.")
    return node


def _validate_source_material(material) -> None:
    """원본 Material을 변경하기 전에 지원 구조인지 검증합니다."""
    if material is None:
        return
    if material.library is not None:
        raise PaintSetupError("Library Link Material은 로컬 복사 후 사용하십시오.")
    if not material.use_nodes or material.node_tree is None:
        raise PaintSetupError("Node를 사용하지 않는 기존 Material은 1차 버전에서 지원하지 않습니다.")
    _supported_principled(material)


def _direct_base_image(material):
    """Base Color에 직접 연결된 Image Texture를 반환합니다."""
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    principled = _supported_principled(material)
    base = principled.inputs["Base Color"]
    if len(base.links) != 1:
        return None
    source = base.links[0].from_node
    return source.image if source.type == "TEX_IMAGE" else None


def resolve_overlay_size(
    material,
    width: int,
    height: int,
    *,
    use_base_resolution: bool = True,
) -> tuple[int, int]:
    """기존 Base Image 또는 UI 값으로 Overlay 해상도를 결정합니다."""
    if width <= 0 or height <= 0:
        raise PaintSetupError("Overlay 해상도는 0보다 커야 합니다.")
    if not use_base_resolution:
        return int(width), int(height)
    image = _direct_base_image(material)
    if image is None:
        return int(width), int(height)
    size = tuple(int(value) for value in image.size)
    return size if len(size) == 2 and min(size) > 0 else (int(width), int(height))


def _new_default_material(target):
    """Base Color 기본값을 가진 새 관리 Material을 생성합니다."""
    material = bpy.data.materials.new(name=f"{target.name}_ProjectionPaint")
    material.use_nodes = True
    _supported_principled(material)
    return material


def _new_managed_material(target, original):
    """원본을 보존하고 Paint용 표면 기본값을 적용한 관리 Material을 생성합니다."""
    material = original.copy() if original is not None else _new_default_material(target)
    material.name = f"{target.name}_ProjectionPaint"
    principled = _supported_principled(material)
    principled.inputs["Roughness"].default_value = 1.0
    principled.inputs["IOR"].default_value = 1.0
    material[OWNER_KEY] = OWNER_TOKEN
    material[ROLE_KEY] = MATERIAL_ROLE
    material[SCHEMA_KEY] = SCHEMA_VERSION
    material[TARGET_KEY] = _object_uuid(target)
    return material


def _new_overlay(
    target,
    width: int,
    height: int,
    tile_numbers: tuple[int, ...] | None = None,
    backend: str | None = None,
):
    """투명 RGBA8 관리 Overlay Image를 생성합니다."""
    numbers = tile_numbers or tuple(
        int(number) for number in target.get(UDIM_TILES_KEY, (1001,))
    )
    resolved_backend = backend or backend_from_target(target)
    image = new_managed_paint_image(
        resolved_backend,
        f"{target.name}_PaintOverlay",
        (int(width), int(height)),
        numbers,
        (0.0, 0.0, 0.0, 0.0),
    )
    image[OWNER_KEY] = OWNER_TOKEN
    image[ROLE_KEY] = OVERLAY_ROLE
    image[UUID_KEY] = uuid.uuid4().hex
    image[SCHEMA_KEY] = SCHEMA_VERSION
    image[TARGET_KEY] = _object_uuid(target)
    try:
        image.colorspace_settings.name = "sRGB"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return image


def _replace_base_source_image(material, image) -> None:
    """복사한 관리 재질의 직접 Base Color Image만 Tiled Source로 교체합니다."""
    if image is None:
        return
    principled = _supported_principled(material)
    base = principled.inputs["Base Color"]
    if len(base.links) != 1 or base.links[0].from_node.type != "TEX_IMAGE":
        raise PaintSetupError("관리 재질의 Base Color Image 연결을 찾지 못했습니다.")
    base.links[0].from_node.image = image


def _tag_node(node, role: str) -> None:
    """관리 Node에 소유권과 역할을 기록합니다."""
    node[OWNER_KEY] = OWNER_TOKEN
    node[ROLE_KEY] = role
    node[SCHEMA_KEY] = SCHEMA_VERSION


def _install_overlay_nodes(material, overlay):
    """기존 Base Color 신호 위에 Alpha Mix Node를 삽입합니다."""
    principled = _supported_principled(material)
    tree = material.node_tree
    base = principled.inputs["Base Color"]
    previous_link = base.links[0] if len(base.links) == 1 else None
    previous_active = tree.nodes.active
    original_texture_node = (
        previous_link.from_node
        if previous_link is not None and previous_link.from_node.type == "TEX_IMAGE"
        else None
    )
    previous_default = tuple(float(value) for value in base.default_value)
    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.name = "PP Paint Overlay"
    image_node.label = "Projection Paint Overlay"
    image_node.image = overlay
    _tag_node(image_node, OVERLAY_NODE_ROLE)
    mix = tree.nodes.new("ShaderNodeMixRGB")
    mix.name = "PP Base Color Overlay"
    mix.label = "Projection Paint Base Color Overlay"
    mix.blend_type = "MIX"
    mix.inputs[1].default_value = previous_default
    _tag_node(mix, MIX_NODE_ROLE)
    if previous_link is not None:
        source_socket = previous_link.from_socket
        tree.links.remove(previous_link)
        tree.links.new(source_socket, mix.inputs[1])
    tree.links.new(image_node.outputs["Color"], mix.inputs[2])
    tree.links.new(image_node.outputs["Alpha"], mix.inputs[0])
    tree.links.new(mix.outputs["Color"], base)
    image_node.select = False
    mix.select = False
    display_node = original_texture_node or previous_active
    if display_node is not None:
        display_node.select = True
        tree.nodes.active = display_node
    return image_node, mix


def _managed_nodes(material, overlay=None):
    """관리 Material에서 지정 Overlay의 Image와 Mix Node를 반환합니다."""
    nodes = tuple(material.node_tree.nodes)
    image_nodes = [
        node
        for node in nodes
        if node.get(OWNER_KEY) == OWNER_TOKEN
        and node.get(ROLE_KEY) == OVERLAY_NODE_ROLE
        and (overlay is None or node.image is overlay)
    ]
    if overlay is None and len(image_nodes) > 1:
        settings = getattr(
            getattr(bpy.context, "scene", None),
            "projection_paint",
            None,
        )
        active = getattr(settings, "overlay_image", None)
        active_matches = [node for node in image_nodes if node.image is active]
        if len(active_matches) == 1:
            image_nodes = active_matches
    if len(image_nodes) != 1:
        raise PaintSetupError("관리 Overlay Image Node를 하나로 확정할 수 없습니다.")
    layer_uuid = str(image_nodes[0].get("pp_layer_uuid", "") or "")
    mix_nodes = tuple(
        node
        for node in nodes
        if node.get(OWNER_KEY) == OWNER_TOKEN
        and node.get(ROLE_KEY) == MIX_NODE_ROLE
        and str(node.get("pp_layer_uuid", "") or "") == layer_uuid
    )
    if len(mix_nodes) != 1:
        raise PaintSetupError("관리 Material Node 구조가 변경되어 자동 복구를 중지했습니다.")
    return image_nodes[0], mix_nodes[0]


def _validate_managed_material(material, overlay=None):
    """관리 Material·Overlay의 소유권과 식별 계약만 변경 없이 검증합니다."""
    try:
        schema_version = (
            int(material.get(SCHEMA_KEY, 0))
            if material is not None
            else 0
        )
    except (TypeError, ValueError):
        schema_version = 0
    if (
        material is None
        or material.get(OWNER_KEY) != OWNER_TOKEN
        or material.get(ROLE_KEY) != MATERIAL_ROLE
        or schema_version != SCHEMA_VERSION
    ):
        raise PaintSetupError("관리 Material의 소유권 또는 Schema가 변경되었습니다.")
    image_node, mix = _managed_nodes(material, overlay)
    resolved_overlay = image_node.image
    if (
        resolved_overlay is None
        or resolved_overlay.get(OWNER_KEY) != OWNER_TOKEN
        or resolved_overlay.get(ROLE_KEY) != OVERLAY_ROLE
    ):
        raise PaintSetupError("관리 Overlay Image 연결이 변경되었습니다.")
    from .layer_shader import has_layer_backend

    layer_backend = has_layer_backend(material)
    if not layer_backend:
        principled = _supported_principled(material)
        base = principled.inputs["Base Color"]
        if len(base.links) != 1 or _pointer(base.links[0].from_node) != _pointer(mix):
            raise PaintSetupError("관리 Overlay의 Base Color 연결이 변경되었습니다.")
    if not layer_backend and (
        len(mix.inputs[0].links) != 1
        or _pointer(mix.inputs[0].links[0].from_node) != _pointer(image_node)
    ):
        raise PaintSetupError("관리 Overlay의 Alpha 연결이 변경되었습니다.")
    if not layer_backend and (
        len(mix.inputs[2].links) != 1
        or _pointer(mix.inputs[2].links[0].from_node) != _pointer(image_node)
    ):
        raise PaintSetupError("관리 Overlay의 Color 연결이 변경되었습니다.")
    return image_node, mix, resolved_overlay


def _managed_base_image(mix_node):
    """관리 Mix Node에 보존된 원본 Base Image를 반환합니다."""
    visited = set()
    current = mix_node
    while current is not None and _pointer(current) not in visited:
        visited.add(_pointer(current))
        base_input = current.inputs[1]
        if len(base_input.links) != 1:
            color_input = current.inputs[2]
            if len(color_input.links) == 1:
                color_source = color_input.links[0].from_node
                if (
                    color_source.type == "TEX_IMAGE"
                    and color_source.image is not None
                    and bool(color_source.image.get("pp_is_original_layer", False))
                ):
                    return color_source.image
            return None
        source = base_input.links[0].from_node
        if (
            source.get(OWNER_KEY) == OWNER_TOKEN
            and source.get(ROLE_KEY) == MIX_NODE_ROLE
        ):
            current = source
            continue
        if (
            source.get(OWNER_KEY) == OWNER_TOKEN
            and source.get(ROLE_KEY) == ORIGINAL_GATE_NODE_ROLE
        ):
            raw_input = source.inputs[2] if len(source.inputs) >= 3 else None
            if raw_input is None or len(raw_input.links) != 1:
                return None
            raw_source = raw_input.links[0].from_node
            return raw_source.image if raw_source.type == "TEX_IMAGE" else None
        return source.image if source.type == "TEX_IMAGE" else None
    return None


def _image_size(image) -> tuple[int, int] | None:
    """유효한 Blender Image 크기를 정수 쌍으로 반환합니다."""
    if image is None:
        return None
    size = tuple(int(value) for value in image.size)
    return size if len(size) == 2 and min(size) > 0 else None


def _managed_udim_record(target):
    """Object 메타데이터에서 검증된 관리 슬롯·재질·UV·타일을 읽습니다."""
    if target is None or _pointer(target) == 0:
        return None
    present = tuple(key for key in _MANAGED_UDIM_METADATA_KEYS if key in target)
    if not present:
        if IMAGE_BACKEND_KEY in target:
            raise PaintSetupError("관리 Paint Image 백엔드 기록만 남아 재변환을 중지했습니다.")
        return None
    missing = tuple(key for key in _MANAGED_UDIM_METADATA_KEYS if key not in target)
    if missing:
        raise PaintSetupError(
            "관리 UDIM 기록이 일부만 남아 재변환을 중지했습니다: "
            + ", ".join(missing)
        )
    try:
        _kind, tile_numbers, _signature = layout_from_target(target)
    except (UDIMSetupError, TypeError, ValueError) as exc:
        raise PaintSetupError(
            f"관리 Target의 UDIM Layout 기록이 유효하지 않습니다: {exc}"
        ) from exc
    try:
        slot_index = int(target.get(UDIM_MANAGED_SLOT_KEY))
        original_slot_index = int(target.get(ORIGINAL_ACTIVE_SLOT_KEY))
    except (TypeError, ValueError) as exc:
        raise PaintSetupError("관리 UDIM Material Slot 기록이 유효하지 않습니다.") from exc
    if slot_index < 0 or slot_index >= len(target.material_slots):
        raise PaintSetupError("관리 UDIM Material Slot이 삭제되었습니다.")
    if original_slot_index < 0 or original_slot_index > slot_index:
        raise PaintSetupError("원본 Material Slot 기록이 유효하지 않습니다.")
    slot = target.material_slots[slot_index]
    material = slot.material
    if material is None:
        raise PaintSetupError("관리 UDIM Material Slot의 재질이 삭제되었습니다.")
    target_uuid = str(target.get(TARGET_KEY, "") or "")
    if not target_uuid:
        raise PaintSetupError("관리 UDIM Target 소유권 UUID가 없습니다.")
    if (
        material.get(OWNER_KEY) != OWNER_TOKEN
        or material.get(ROLE_KEY) != MATERIAL_ROLE
    ):
        raise PaintSetupError("관리 UDIM Material Slot의 재질 또는 소유 태그가 변경되었습니다.")
    if str(material.get(TARGET_KEY, "") or "") != target_uuid:
        raise PaintSetupError("관리 UDIM Material의 Target 소유권이 변경되었습니다.")
    uv_name = str(target.get(UDIM_UV_KEY, "") or "")
    if uv_name != PAINT_UV_NAME:
        raise PaintSetupError("관리 PP_UDIM UV 기록이 변경되었습니다.")
    uv = target.data.uv_layers.get(uv_name)
    if uv is None or _pointer(_active_uv(target)) != _pointer(uv):
        raise PaintSetupError("관리 PP_UDIM UV가 활성 상태가 아닙니다.")
    source_uv_name = str(target.get(UDIM_SOURCE_UV_KEY, "") or "")
    if not source_uv_name or target.data.uv_layers.get(source_uv_name) is None:
        raise PaintSetupError("원본 UDIM Source UV 기록이 변경되었습니다.")
    face_attribute = target.data.attributes.get(ORIGINAL_FACE_ATTRIBUTE)
    if (
        face_attribute is None
        or str(face_attribute.domain) != "FACE"
        or str(face_attribute.data_type) != "INT"
        or len(face_attribute.data) != len(target.data.polygons)
    ):
        raise PaintSetupError("원본 Material Index 백업이 변경되었습니다.")
    return slot_index, slot, material, uv, tile_numbers


def _recoverable_file_overlay(
    target,
    overlay,
    tile_numbers: tuple[int, ...],
    overlay_tiles: tuple[int, ...],
) -> bool:
    """단일 1001 관리 Image가 안전한 Tiled 교체 후보인지 읽기 전용으로 판정합니다."""
    if (
        IMAGE_BACKEND_KEY in target
        or
        str(target.get(UDIM_KIND_KEY, "") or "") != "SINGLE_1001"
        or tile_numbers != (1001,)
        or overlay_tiles != (1001,)
        or str(getattr(overlay, "source", "")) != "FILE"
        or getattr(overlay, "library", None) is not None
        or getattr(overlay, "override_library", None) is not None
    ):
        return False
    size = _image_size(overlay)
    if size is None:
        return False
    has_packed_data = bool(
        getattr(overlay, "packed_file", None) is not None
        or len(tuple(getattr(overlay, "packed_files", ()))) > 0
    )
    has_external_path = bool(
        str(getattr(overlay, "filepath", "") or getattr(overlay, "filepath_raw", ""))
    )
    try:
        has_pixels = len(overlay.pixels) == int(size[0]) * int(size[1]) * 4
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        has_pixels = False
    return bool(has_packed_data or has_external_path or has_pixels)


def classify_managed_overlay(target, overlay=None) -> ManagedOverlayClassification:
    """관리 Overlay를 정상·단일 1001 복구 가능·손상으로 나눠 읽기 전용 반환합니다."""
    try:
        record = _managed_udim_record(target)
        if record is None:
            return ManagedOverlayClassification(
                MANAGED_OVERLAY_INVALID,
                "관리 UDIM 기록이 없습니다.",
            )
        _slot_index, _slot, material, _uv, tile_numbers = record
        _image_node, _mix, resolved_overlay = _validate_managed_material(
            material,
            overlay,
        )
        target_uuid = str(target.get(TARGET_KEY, "") or "")
        overlay_schema = int(resolved_overlay.get(SCHEMA_KEY, 0))
        overlay_tiles = tuple(int(tile.number) for tile in resolved_overlay.tiles)
        source = str(resolved_overlay.source)
        if (
            str(resolved_overlay.get(TARGET_KEY, "") or "") != target_uuid
            or overlay_schema != SCHEMA_VERSION
        ):
            return ManagedOverlayClassification(
                MANAGED_OVERLAY_INVALID,
                "관리 Overlay의 Target 소유권이 변경되었습니다.",
                tile_numbers,
                overlay_tiles,
                source,
            )
        explicit_backend = str(target.get(IMAGE_BACKEND_KEY, "") or "")
        try:
            expected_backend = backend_from_target(target, resolved_overlay)
            actual_contract = image_contract(resolved_overlay)
        except PaintImageContractError as exc:
            return ManagedOverlayClassification(
                MANAGED_OVERLAY_INVALID,
                str(exc),
                tile_numbers,
                overlay_tiles,
                source,
            )
        if actual_contract.backend == expected_backend:
            if actual_contract.tile_numbers != tile_numbers:
                return ManagedOverlayClassification(
                    MANAGED_OVERLAY_INVALID,
                    "관리 Overlay 논리 타일이 다릅니다: "
                    f"기대 {list(tile_numbers)}, 실제 {list(actual_contract.tile_numbers)}",
                    tile_numbers,
                    actual_contract.tile_numbers,
                    source,
                )
            return ManagedOverlayClassification(
                MANAGED_OVERLAY_VALID if explicit_backend else MANAGED_OVERLAY_LEGACY_VALID,
                "",
                tile_numbers,
                actual_contract.tile_numbers,
                source,
            )
        if _recoverable_file_overlay(
            target,
            resolved_overlay,
            tile_numbers,
            overlay_tiles,
        ):
            return ManagedOverlayClassification(
                MANAGED_OVERLAY_RECOVERABLE_FILE_1001,
                "관리 Overlay가 단일 이미지 형식이라 UDIM 1001 변환이 필요합니다.",
                tile_numbers,
                overlay_tiles,
                source,
            )
        return ManagedOverlayClassification(
            MANAGED_OVERLAY_INVALID,
            "관리 Overlay Image 형식이 유효하지 않습니다: "
            f"기대 TILED, 실제 {source or 'UNKNOWN'}",
            tile_numbers,
            overlay_tiles,
            source,
        )
    except PaintSetupError as exc:
        return ManagedOverlayClassification(MANAGED_OVERLAY_INVALID, str(exc))
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        return ManagedOverlayClassification(
            MANAGED_OVERLAY_INVALID,
            f"관리 Overlay의 UDIM 기록이 유효하지 않습니다: {exc}",
        )


def managed_overlay_recovery_required(target, overlay=None) -> bool:
    """제어 가능한 사용자 작업 전에 단일 1001 Image 변환이 필요한지 반환합니다."""
    return (
        classify_managed_overlay(target, overlay).state
        == MANAGED_OVERLAY_RECOVERABLE_FILE_1001
    )


def _managed_udim_contract(
    target,
    overlay=None,
    *,
    allow_recoverable_file: bool = False,
):
    """저장된 UDIM 기록과 실제 관리 재질·Overlay 계약을 함께 검증합니다."""
    record = _managed_udim_record(target)
    if record is None:
        return None
    slot_index, slot, material, uv, tile_numbers = record
    image_node, mix, resolved_overlay = _validate_managed_material(material, overlay)
    target_uuid = str(target.get(TARGET_KEY, "") or "")
    try:
        overlay_schema = int(resolved_overlay.get(SCHEMA_KEY, 0))
        overlay_tiles = tuple(int(tile.number) for tile in resolved_overlay.tiles)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PaintSetupError("관리 Overlay의 UDIM 기록이 유효하지 않습니다.") from exc
    if (
        str(resolved_overlay.get(TARGET_KEY, "") or "") != target_uuid
        or overlay_schema != SCHEMA_VERSION
    ):
        raise PaintSetupError("관리 Overlay의 Target 소유권이 변경되었습니다.")
    source = str(resolved_overlay.source)
    try:
        expected_backend = backend_from_target(target, resolved_overlay)
        actual_contract = image_contract(resolved_overlay)
    except PaintImageContractError as exc:
        raise PaintSetupError(str(exc)) from exc
    if actual_contract.backend != expected_backend:
        if not (
            allow_recoverable_file
            and _recoverable_file_overlay(
                target,
                resolved_overlay,
                tile_numbers,
                overlay_tiles,
            )
        ):
            raise PaintSetupError(
                "관리 Overlay Image 형식이 유효하지 않습니다: "
                f"기대 {expected_backend}, 실제 {actual_contract.backend}"
            )
    elif actual_contract.tile_numbers != tile_numbers:
        raise PaintSetupError(
            "관리 Overlay 논리 타일이 다릅니다: "
            f"기대 {list(tile_numbers)}, 실제 {list(actual_contract.tile_numbers)}"
        )
    if IMAGE_BACKEND_KEY in target:
        try:
            backend_from_target(target, allow_legacy=False)
        except PaintImageContractError as exc:
            raise PaintSetupError(str(exc)) from exc
    return slot_index, slot, material, uv, image_node, mix, resolved_overlay


def requires_udim_conversion(target) -> bool:
    """관리 기록이 전혀 없는 Target에만 최초 UDIM 변환을 허용합니다."""
    return _managed_udim_contract(target, allow_recoverable_file=True) is None


def pending_overlay_resolution_change(
    target,
    width: int,
    height: int,
    *,
    use_base_resolution: bool = True,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """기존 관리 Overlay와 진입 시 적용할 해상도가 다르면 두 크기를 반환합니다."""
    if width <= 0 or height <= 0:
        raise PaintSetupError("Overlay 해상도는 0보다 커야 합니다.")
    if target is None or _pointer(target) == 0:
        return None
    contract = _managed_udim_contract(target, allow_recoverable_file=True)
    if contract is None:
        return None
    _slot_index, _slot, _material, _uv, _image_node, mix, overlay = contract
    desired = (int(width), int(height))
    if use_base_resolution:
        desired = _image_size(_managed_base_image(mix)) or desired
    current = _image_size(overlay)
    if current is None:
        raise PaintSetupError("기존 Paint Overlay의 해상도가 유효하지 않습니다.")
    return (current, desired) if current != desired else None


@dataclass
class PaintMaterialSetup:
    """Paint Target과 관리 Material·Overlay의 불변 계약을 보관합니다."""

    target: object
    mesh: object
    material: object
    overlay: object
    image_node: object
    mix_node: object
    slot_index: int
    slot_link: str
    uv_name: str
    target_pointer: int
    mesh_pointer: int
    material_pointer: int
    overlay_pointer: int
    image_backend: str
    overlay_contract: PaintImageContract
    original_material: object | None = None
    original_active_index: int = 0
    slot_appended: bool = False
    created: bool = False
    target_uuid_created: bool = False
    replaced_overlay: object | None = None
    original_face_indices: tuple[int, ...] = ()
    original_uv_name: str = ""
    original_render_uv_names: tuple[str, ...] = ()
    created_uv: object | None = None
    created_face_attribute: object | None = None
    created_source_image: object | None = None
    target_property_snapshot: dict[str, tuple[bool, object]] | None = None
    committed: bool = False

    def validate(self) -> None:
        """현재 Blender 상태가 진입 시 Material 계약과 같은지 검증합니다."""
        validate_material_contract(self)

    def commit(self) -> None:
        """관리 Material과 Overlay를 영구 사용자 결과물로 확정합니다."""
        previous = self.replaced_overlay
        if previous is not None:
            from .overlay_durability import unregister_overlay

            unregister_overlay(previous)
            try:
                _remove_unused_paint_image(previous, allow_dirty=True)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
            self.replaced_overlay = None
        self.committed = True

    def rollback(self) -> None:
        """실패한 생성 또는 Overlay 교체 Transaction을 사용자 상태로 되돌립니다."""
        if self.committed:
            return
        if self.replaced_overlay is not None:
            previous = self.replaced_overlay
            try:
                if _pointer(self.image_node) and self.image_node.image is self.overlay:
                    self.image_node.image = previous
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
            for scene in tuple(bpy.data.scenes):
                settings = getattr(scene, "projection_paint", None)
                if settings is not None and settings.overlay_image is self.overlay:
                    settings.overlay_image = previous
            from .overlay_durability import register_overlay, unregister_overlay

            try:
                unregister_overlay(self.overlay)
                register_overlay(previous)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
            try:
                _remove_unused_paint_image(self.overlay, allow_dirty=True)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
            self.replaced_overlay = None
            return
        if not self.created:
            return
        try:
            from .overlay_durability import unregister_overlay

            unregister_overlay(self.overlay)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        try:
            if _pointer(self.target) and _pointer(self.mesh):
                if self.original_face_indices:
                    for polygon, old_index in zip(
                        self.mesh.polygons,
                        self.original_face_indices,
                    ):
                        polygon.material_index = int(old_index)
                original_uv = self.mesh.uv_layers.get(self.original_uv_name)
                if original_uv is not None:
                    self.mesh.uv_layers.active = original_uv
                render_names = set(self.original_render_uv_names)
                for layer in self.mesh.uv_layers:
                    layer.active_render = str(layer.name) in render_names
                if self.slot_appended:
                    if (
                        self.slot_index < len(self.mesh.materials)
                        and _pointer(self.mesh.materials[self.slot_index])
                        == _pointer(self.material)
                    ):
                        self.mesh.materials.pop(index=self.slot_index)
                elif self.slot_index < len(self.target.material_slots):
                    slot = self.target.material_slots[self.slot_index]
                    if slot.material is self.material:
                        slot.material = self.original_material
                if len(self.target.material_slots):
                    self.target.active_material_index = min(
                        self.original_active_index,
                        len(self.target.material_slots) - 1,
                    )
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
        try:
            if self.created_face_attribute is not None:
                attribute = self.mesh.attributes.get(ORIGINAL_FACE_ATTRIBUTE)
                if _pointer(attribute) == _pointer(self.created_face_attribute):
                    self.mesh.attributes.remove(attribute)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
        try:
            if self.created_uv is not None:
                uv = self.mesh.uv_layers.get(str(self.created_uv.name))
                if _pointer(uv) == _pointer(self.created_uv):
                    self.mesh.uv_layers.remove(uv)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
        try:
            for key, (existed, value) in (self.target_property_snapshot or {}).items():
                if existed:
                    self.target[key] = value
                elif key in self.target:
                    del self.target[key]
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
        try:
            if _pointer(self.material) and self.material.users == 0:
                bpy.data.materials.remove(self.material)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        try:
            _remove_unused_paint_image(self.overlay, allow_dirty=False)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        try:
            if (
                self.created_source_image is not None
                and _pointer(self.created_source_image)
                and self.created_source_image.users == 0
            ):
                bpy.data.images.remove(self.created_source_image)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        try:
            if self.target_uuid_created and TARGET_KEY in self.target:
                del self.target[TARGET_KEY]
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass


def _setup_from_managed(target, material, overlay=None) -> PaintMaterialSetup:
    """기존 관리 Material을 재구성하지 않고 재사용합니다."""
    contract = _managed_udim_contract(target, overlay)
    if contract is None:
        raise PaintSetupError("관리 UDIM 기록이 없어 기존 구조를 재사용할 수 없습니다.")
    slot_index, slot, resolved_material, uv, image_node, mix, resolved_overlay = contract
    if material is not None and _pointer(material) != _pointer(resolved_material):
        raise PaintSetupError("요청한 관리 Material이 저장된 UDIM Material과 다릅니다.")
    setup = PaintMaterialSetup(
        target=target,
        mesh=target.data,
        material=resolved_material,
        overlay=resolved_overlay,
        image_node=image_node,
        mix_node=mix,
        slot_index=slot_index,
        slot_link=str(slot.link),
        uv_name=str(uv.name),
        target_pointer=_pointer(target),
        mesh_pointer=_pointer(target.data),
        material_pointer=_pointer(resolved_material),
        overlay_pointer=_pointer(resolved_overlay),
        image_backend=backend_from_target(target, resolved_overlay),
        overlay_contract=image_contract(resolved_overlay),
    )
    target.active_material_index = slot_index
    return setup


def _target_property_snapshot(target) -> dict[str, tuple[bool, object]]:
    """UDIM 전환이 변경할 Object 속성의 이전 값을 보관합니다."""
    keys = (
        IMAGE_BACKEND_KEY,
        UDIM_KIND_KEY,
        UDIM_TILES_KEY,
        UDIM_SIGNATURE_KEY,
        UDIM_UV_KEY,
        UDIM_SOURCE_UV_KEY,
        UDIM_MANAGED_SLOT_KEY,
        ORIGINAL_ACTIVE_SLOT_KEY,
    )
    return {
        key: (key in target, target.get(key))
        for key in keys
    }


def _write_layout_properties(target, layout, slot_index: int) -> None:
    """검증이 끝난 UDIM Layout과 관리 슬롯을 Object에 기록합니다."""
    target[IMAGE_BACKEND_KEY] = backend_from_layout(layout)
    target[UDIM_KIND_KEY] = str(layout.kind)
    target[UDIM_TILES_KEY] = list(layout.tile_numbers)
    target[UDIM_SIGNATURE_KEY] = str(layout.signature)
    target[UDIM_UV_KEY] = PAINT_UV_NAME
    target[UDIM_SOURCE_UV_KEY] = str(layout.source_uv_name)
    target[UDIM_MANAGED_SLOT_KEY] = int(slot_index)


def setup_from_scene_state(scene) -> PaintMaterialSetup:
    """Scene Pointer가 가리키는 관리 Material 계약을 짧은 Snapshot으로 복원합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        raise PaintSetupError("Projection Paint Scene 설정이 없습니다.")
    target = settings.paint_target
    material = settings.paint_material
    overlay = settings.overlay_image
    validate_paint_target(target)
    if settings.paint_mesh_uuid and str(
        target.data.get(PAINT_MESH_UUID_KEY, "") or ""
    ) != str(settings.paint_mesh_uuid):
        raise PaintSetupError("Paint Target의 Mesh가 변경되었습니다.")
    uv = _active_uv(target)
    if settings.paint_uv_name and str(uv.name) != str(settings.paint_uv_name):
        raise PaintSetupError("활성 UV Map이 변경되었습니다.")
    if int(target.active_material_index) != int(settings.paint_material_slot_index):
        raise PaintSetupError("활성 Material Slot이 변경되었습니다.")
    slot_index = int(settings.paint_material_slot_index)
    if slot_index < 0 or slot_index >= len(target.material_slots):
        raise PaintSetupError("관리 Material Slot이 삭제되었습니다.")
    slot = target.material_slots[slot_index]
    if settings.paint_material_slot_link and str(slot.link) != str(
        settings.paint_material_slot_link
    ):
        raise PaintSetupError("관리 Material Slot Link가 변경되었습니다.")
    if material is None or target.active_material is not material:
        raise PaintSetupError("Paint Target의 활성 Material이 관리 Material과 다릅니다.")
    setup = _setup_from_managed(target, material, overlay)
    if overlay is None or setup.overlay is not overlay:
        raise PaintSetupError("Scene의 권위 Overlay와 Material Overlay가 다릅니다.")
    return setup


def _prepare_active_material_steps(
    target,
    width: int = 2048,
    height: int = 2048,
    *,
    use_base_resolution: bool = True,
    replace_existing_overlay: bool = False,
) -> Iterator[PaintMaterialProgress]:
    """Target을 단일 UDIM 구조로 준비하며 실제 단계마다 진행값을 내보냅니다."""
    validate_paint_target(target)
    yield PaintMaterialProgress(
        "Analyzing materials and UV",
        0,
        11,
        f"대상: {target.name} · 폴리곤 {len(target.data.polygons):,}개",
    )
    managed_record = _managed_udim_record(target)
    if managed_record is not None:
        current = managed_record[2]
        settings = getattr(
            getattr(bpy.context, "scene", None),
            "projection_paint",
            None,
        )
        overlay_hint = (
            settings.overlay_image
            if settings is not None and settings.paint_material is current
            else None
        )
        setup = _setup_from_managed(target, current, overlay_hint)
        change = pending_overlay_resolution_change(
            target,
            width,
            height,
            use_base_resolution=use_base_resolution,
        )
        if replace_existing_overlay:
            desired_size = (
                change[1]
                if change is not None
                else _image_size(setup.overlay)
            )
            if desired_size is None:
                raise PaintSetupError("새 Paint Overlay 해상도가 유효하지 않습니다.")
            replacement = _new_overlay(
                target,
                *desired_size,
                backend=setup.image_backend,
            )
            previous = setup.overlay
            setup.image_node.image = replacement
            setup.overlay = replacement
            setup.overlay_pointer = _pointer(replacement)
            setup.overlay_contract = image_contract(replacement)
            setup.replaced_overlay = previous
            try:
                _validate_managed_material(current, replacement)
            except Exception:
                setup.rollback()
                raise
        from .overlay_durability import register_overlay

        register_overlay(setup.overlay)
        return setup
    try:
        layout = analyze_udim_source(target)
    except UDIMSetupError as exc:
        raise PaintSetupError(str(exc)) from exc
    for source in layout.sources:
        _validate_source_material(source.material)
    detail = (
        f"타일 {len(layout.tiles)}개 · 폴리곤 {len(target.data.polygons):,}개"
    )
    yield PaintMaterialProgress(
        "Validating source materials",
        1,
        11,
        detail,
    )
    representative = next(
        (source.material for source in layout.sources if source.material is not None),
        None,
    )
    yield PaintMaterialProgress(
        "Capturing mesh state",
        2,
        11,
        detail,
    )
    original_index = int(target.active_material_index)
    target_uuid_created = not bool(str(target.get(TARGET_KEY, "")))
    slot_index = len(target.material_slots)
    property_snapshot = _target_property_snapshot(target)
    original_faces = tuple(int(polygon.material_index) for polygon in target.data.polygons)
    original_uv = _active_uv(target)
    original_render_uv_names = tuple(
        str(layer.name) for layer in target.data.uv_layers if bool(layer.active_render)
    )
    managed = None
    overlay = None
    source_image = None
    source_image_created = False
    paint_uv = None
    face_attribute = None
    setup = None
    try:
        resolved_width, resolved_height = paint_image_size(
            layout,
            width,
            height,
            use_base_resolution=use_base_resolution,
        )
        yield PaintMaterialProgress(
            "Creating managed material",
            3,
            11,
            detail,
        )
        managed = _new_managed_material(target, representative)
        yield PaintMaterialProgress(
            "Loading paint source",
            4,
            11,
            detail,
        )
        source_image, source_image_created = prepare_udim_source_image(
            layout,
            f"{target.name}_Original_UDIM",
        )
        if layout.kind == "MULTI_MATERIAL_TILES":
            _replace_base_source_image(managed, source_image)
        yield PaintMaterialProgress(
            "Creating paint image",
            5,
            11,
            (
                f"타일 {len(layout.tiles)}개 · "
                f"해상도 {resolved_width:,} × {resolved_height:,}"
            ),
        )
        overlay = _new_overlay(
            target,
            resolved_width,
            resolved_height,
            layout.tile_numbers,
            backend_from_layout(layout),
        )
        yield PaintMaterialProgress(
            "Building shader layer",
            6,
            11,
            detail,
        )
        image_node, mix = _install_overlay_nodes(managed, overlay)
        yield PaintMaterialProgress(
            "Creating PP_UDIM UV",
            7,
            11,
            detail,
        )
        paint_uv = build_paint_uv(target, layout)
        yield PaintMaterialProgress(
            "Backing up material assignments",
            8,
            11,
            detail,
        )
        face_attribute = write_original_face_indices(target, original_faces)
        yield PaintMaterialProgress(
            "Applying single material",
            9,
            11,
            detail,
        )
        target.data.materials.append(managed)
        for polygon in target.data.polygons:
            polygon.material_index = slot_index
        target.active_material_index = slot_index
        slot_link = str(target.material_slots[slot_index].link)
        target[ORIGINAL_ACTIVE_SLOT_KEY] = int(original_index)
        _write_layout_properties(target, layout, slot_index)
        setup = PaintMaterialSetup(
            target=target,
            mesh=target.data,
            material=managed,
            overlay=overlay,
            image_node=image_node,
            mix_node=mix,
            slot_index=slot_index,
            slot_link=slot_link,
            uv_name=str(paint_uv.name),
            target_pointer=_pointer(target),
            mesh_pointer=_pointer(target.data),
            material_pointer=_pointer(managed),
            overlay_pointer=_pointer(overlay),
            image_backend=backend_from_layout(layout),
            overlay_contract=image_contract(overlay),
            original_material=representative,
            original_active_index=original_index,
            slot_appended=True,
            created=True,
            target_uuid_created=target_uuid_created,
            original_face_indices=original_faces,
            original_uv_name=str(original_uv.name),
            original_render_uv_names=original_render_uv_names,
            created_uv=paint_uv,
            created_face_attribute=face_attribute,
            created_source_image=(source_image if source_image_created else None),
            target_property_snapshot=property_snapshot,
        )
        yield PaintMaterialProgress(
            "Validating paint preparation",
            10,
            11,
            detail,
        )
        _validate_managed_material(managed)
        try:
            validate_image_contract(overlay, setup.overlay_contract)
        except PaintImageContractError as exc:
            raise PaintSetupError(str(exc)) from exc
        if setup.overlay_contract.tile_numbers != layout.tile_numbers:
            raise PaintSetupError("관리 Overlay의 논리 타일 집합이 Layout과 다릅니다.")
        from .overlay_durability import register_overlay

        register_overlay(overlay)
        yield PaintMaterialProgress(
            "Texture Paint ready",
            11,
            11,
            detail,
        )
        return setup
    except (Exception, GeneratorExit):
        if setup is not None:
            setup.rollback()
        else:
            try:
                for polygon, old_index in zip(target.data.polygons, original_faces):
                    polygon.material_index = int(old_index)
                if original_uv is not None:
                    target.data.uv_layers.active = original_uv
                render_names = set(original_render_uv_names)
                for layer in target.data.uv_layers:
                    layer.active_render = str(layer.name) in render_names
                if len(target.material_slots):
                    target.active_material_index = min(
                        original_index,
                        len(target.material_slots) - 1,
                    )
                if (
                    managed is not None
                    and slot_index < len(target.data.materials)
                    and _pointer(target.data.materials[slot_index]) == _pointer(managed)
                ):
                    target.data.materials.pop(index=slot_index)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
            try:
                if managed is not None and managed.users == 0:
                    bpy.data.materials.remove(managed)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
            try:
                _remove_unused_paint_image(overlay, allow_dirty=False)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
            try:
                if (
                    paint_uv is not None
                    and _pointer(target.data.uv_layers.get(str(paint_uv.name)))
                    == _pointer(paint_uv)
                ):
                    target.data.uv_layers.remove(paint_uv)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
            try:
                if face_attribute is not None:
                    attribute = target.data.attributes.get(ORIGINAL_FACE_ATTRIBUTE)
                    if _pointer(attribute) == _pointer(face_attribute):
                        target.data.attributes.remove(attribute)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
            try:
                if source_image_created and source_image is not None and source_image.users == 0:
                    bpy.data.images.remove(source_image)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
            try:
                for key, (existed, value) in property_snapshot.items():
                    if existed:
                        target[key] = value
                    elif key in target:
                        del target[key]
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
            try:
                if target_uuid_created and TARGET_KEY in target:
                    del target[TARGET_KEY]
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        raise


def create_paint_material_preparation_job(
    target,
    width: int = 2048,
    height: int = 2048,
    *,
    use_base_resolution: bool = True,
    replace_existing_overlay: bool = False,
) -> PaintMaterialPreparationJob:
    """동기 실행과 Modal UI가 공유할 Paint Material 준비 작업을 생성합니다."""

    return PaintMaterialPreparationJob(
        target,
        width,
        height,
        use_base_resolution=use_base_resolution,
        replace_existing_overlay=replace_existing_overlay,
    )


def prepare_active_material(
    target,
    width: int = 2048,
    height: int = 2048,
    *,
    use_base_resolution: bool = True,
    replace_existing_overlay: bool = False,
) -> PaintMaterialSetup:
    """Paint 준비 작업을 끝까지 실행해 기존 동기 호출 계약을 유지합니다."""

    job = create_paint_material_preparation_job(
        target,
        width,
        height,
        use_base_resolution=use_base_resolution,
        replace_existing_overlay=replace_existing_overlay,
    )
    while not job.done:
        job.advance()
    if job.result is None:
        raise PaintSetupError("UDIM Material 준비 결과가 없습니다.")
    return job.result


def validate_material_contract(setup: PaintMaterialSetup) -> None:
    """세션 중 Target·UV·Material·Overlay가 바뀌지 않았는지 검증합니다."""
    target = setup.target
    if _pointer(target) != setup.target_pointer:
        raise PaintSetupError("Paint Target이 삭제되었습니다.")
    if _pointer(target.data) != setup.mesh_pointer:
        raise PaintSetupError("Paint Target의 Mesh가 변경되었습니다.")
    try:
        _kind, tile_numbers, _signature = layout_from_target(target)
    except UDIMSetupError as exc:
        raise PaintSetupError(str(exc)) from exc
    uv = _active_uv(target)
    if (
        uv is None
        or str(uv.name) != setup.uv_name
        or str(target.get(UDIM_UV_KEY, "") or "") != setup.uv_name
    ):
        raise PaintSetupError("활성 UV Map이 변경되었습니다.")
    if int(target.active_material_index) != setup.slot_index:
        raise PaintSetupError("활성 Material Slot이 변경되었습니다.")
    if setup.slot_index >= len(target.material_slots):
        raise PaintSetupError("관리 Material Slot이 삭제되었습니다.")
    slot = target.material_slots[setup.slot_index]
    if str(slot.link) != setup.slot_link or _pointer(slot.material) != setup.material_pointer:
        raise PaintSetupError("관리 Material Slot 연결이 변경되었습니다.")
    image_node, mix, overlay = _validate_managed_material(
        setup.material,
        setup.overlay,
    )
    if (
        _pointer(image_node) != _pointer(setup.image_node)
        or _pointer(mix) != _pointer(setup.mix_node)
        or _pointer(overlay) != setup.overlay_pointer
    ):
        raise PaintSetupError("관리 Overlay 계약이 변경되었습니다.")
    try:
        current_backend = backend_from_target(target, overlay)
        if current_backend != setup.image_backend:
            raise PaintSetupError(
                "관리 Target의 Paint Image 백엔드가 변경되었습니다: "
                f"기대 {setup.image_backend}, 실제 {current_backend}"
            )
        actual_contract = validate_image_contract(overlay, setup.overlay_contract)
    except PaintImageContractError as exc:
        raise PaintSetupError(str(exc)) from exc
    if actual_contract.tile_numbers != tile_numbers:
        raise PaintSetupError(
            "관리 Overlay 논리 타일이 다릅니다: "
            f"기대 {list(tile_numbers)}, 실제 {list(actual_contract.tile_numbers)}"
        )


__all__ = [
    "MATERIAL_ROLE",
    "MANAGED_OVERLAY_INVALID",
    "MANAGED_OVERLAY_LEGACY_VALID",
    "MANAGED_OVERLAY_RECOVERABLE_FILE_1001",
    "MANAGED_OVERLAY_VALID",
    "ManagedOverlayClassification",
    "OVERLAY_ROLE",
    "OWNER_KEY",
    "OWNER_TOKEN",
    "PaintMaterialPreparationJob",
    "PaintMaterialProgress",
    "PaintMaterialSetup",
    "PaintSetupError",
    "classify_managed_overlay",
    "create_paint_material_preparation_job",
    "managed_overlay_recovery_required",
    "pending_overlay_resolution_change",
    "prepare_active_material",
    "requires_udim_conversion",
    "resolve_overlay_size",
    "setup_from_scene_state",
    "validate_material_contract",
    "validate_paint_target",
]
