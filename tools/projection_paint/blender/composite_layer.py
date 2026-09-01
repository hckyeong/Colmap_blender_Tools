"""Composite 작업 Layer의 Bake·복구 Transaction과 소유권 검증을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from time import monotonic
from types import SimpleNamespace
from typing import Any

import bpy
import numpy as np

from .composite_progress import (
    begin_composite_native_bake,
    create_composite_progress,
    end_composite_native_bake,
    release_composite_progress,
    update_composite_progress,
)
from .layer_shader import (
    COMPOSITE_LAYER_ROLE,
    COMBINED_RGBA_IMAGE_KEY,
    LAYER_IMAGE_ROLE,
    LAYER_IMAGE_NODE_ROLE,
    LAYER_MIX_NODE_ROLE,
    LAYER_UUID_KEY,
    MASK_IMAGE_ROLE,
    NORMAL_LAYER_ROLE,
    LayerShaderError,
    activate_layer_paint_image,
    active_base_color_graph_layers,
    composite_chain_layers,
    composite_session_is_active,
    composite_source_layers,
    create_composite_layer_image,
    detach_shared_owned_image,
    find_layer,
    _remove_image,
    original_layer_is_read_only,
    reconcile_layer_graph,
    remove_owned_layer_bundles,
    remove_adjustment_resources,
    remove_mask_resources,
    validate_active_layer_graph,
    validate_layer_graph,
)
from .ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY, new_uuid
from .image_backend import (
    PaintImageContract,
    PaintImageContractError,
    SINGLE_BACKEND,
    image_contract,
)


COMPOSITE_IMAGE_SESSION_KEY = "pp_composite_session_uuid"
RECOMPOSITE_TRANSACTION_KEY = "pp_recomposite_transaction_uuid"
RECOMPOSITE_PARENT_SESSION_KEY = "pp_recomposite_parent_session_uuid"
RECOMPOSITE_TRANSACTION_KIND_KEY = "pp_recomposite_transaction_kind"
COMPOSITE_BAKE_SAMPLES = 1
COMPOSITE_BAKE_MARGIN_DIVISOR = 256
COMPOSITE_BAKE_MARGIN_MIN = 2
COMPOSITE_BAKE_MARGIN_MAX = 64
COMPOSITE_SOURCE_SCHEMA_VERSION = 1
_CREATE_JOBS: dict[str, "CompositeCreateJob"] = {}


class CompositeLayerError(RuntimeError):
    """Composite 작업 Layer의 계약 또는 Blender Bake 실패를 나타냅니다."""


def _pointer(value: Any) -> int:
    """삭제됐거나 무효한 Blender RNA Pointer를 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _composite_bake_margin(image: Any) -> int:
    """Paint Image 계약의 논리 버퍼 해상도에 비례한 UV 여백을 계산합니다."""
    if not _pointer(image):
        raise CompositeLayerError("Composite Bake Margin을 계산할 Image가 없습니다.")
    contract = _paint_image_contract(image)
    if len(set(contract.tile_sizes)) != 1:
        raise CompositeLayerError("Composite Bake 논리 버퍼 해상도가 서로 다릅니다.")
    reference_size = max(contract.tile_sizes[0])
    scaled_margin = ceil(reference_size / COMPOSITE_BAKE_MARGIN_DIVISOR)
    return max(COMPOSITE_BAKE_MARGIN_MIN, min(COMPOSITE_BAKE_MARGIN_MAX, scaled_margin))


def _settings(scene: Any) -> Any:
    """Scene에 저장된 Projection Painting 설정을 반환합니다."""
    return getattr(scene, "projection_paint", None)


def _set_status(settings: Any, message: str, kind: str) -> None:
    """Composite 작업 결과를 기존 패널 상태 문자열에 기록합니다."""
    settings.status = str(message)
    settings.status_kind = str(kind)


def _owned_layer_image(image: Any, layer_uuid: str) -> bool:
    """Image가 지정 Layer UUID를 가진 애드온 소유 Paint Image인지 확인합니다."""
    try:
        return bool(
            _pointer(image)
            and image.get(OWNER_KEY) == OWNER_TOKEN
            and image.get(RECORD_KIND_KEY) == LAYER_IMAGE_ROLE
            and str(image.get(LAYER_UUID_KEY, "") or "") == str(layer_uuid)
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _mark_recomposite_temporary_image(image: Any, job: "CompositeCreateJob", kind: str) -> None:
    """재합성 중단 복구가 새 Bake Image를 정확히 식별할 Transaction 표식을 기록합니다."""
    if not _pointer(image) or job.old_session is None:
        raise CompositeLayerError("재합성 임시 Image 표식에 필요한 세션이 없습니다.")
    image[COMPOSITE_IMAGE_SESSION_KEY] = str(job.session_uuid)
    image[RECOMPOSITE_TRANSACTION_KEY] = str(job.session_uuid)
    image[RECOMPOSITE_PARENT_SESSION_KEY] = str(job.old_session.session_uuid)
    image[RECOMPOSITE_TRANSACTION_KIND_KEY] = str(kind)


def _clear_recomposite_temporary_image_markers(image: Any) -> None:
    """성공·실패·Load 복구 뒤 남은 재합성 전용 Image 표식을 제거합니다."""
    if not _pointer(image):
        return
    for key in (
        RECOMPOSITE_TRANSACTION_KEY,
        RECOMPOSITE_PARENT_SESSION_KEY,
        RECOMPOSITE_TRANSACTION_KIND_KEY,
        "pp_recomposite_pending_delete",
    ):
        image.pop(key, None)


def _active_output_and_principled(material: Any) -> tuple[Any, Any]:
    """Bake 중 임시 Emission 연결에 사용할 활성 Output과 Principled를 찾습니다."""
    tree = getattr(material, "node_tree", None)
    outputs = tuple(
        node
        for node in getattr(tree, "nodes", ())
        if node.type == "OUTPUT_MATERIAL" and bool(node.is_active_output)
    )
    if len(outputs) != 1:
        raise CompositeLayerError("활성 Material Output이 정확히 하나여야 합니다.")
    surface = outputs[0].inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        raise CompositeLayerError("Material Output Surface 연결이 유효하지 않습니다.")
    principled = surface.links[0].from_node
    if principled is None or principled.type != "BSDF_PRINCIPLED":
        raise CompositeLayerError("Composite Bake에는 직접 연결된 Principled BSDF가 필요합니다.")
    return outputs[0], principled


def _tile_memory_bytes(image: Any) -> int:
    """SINGLE 또는 UDIM 논리 버퍼의 RGBA8 최소 메모리를 계산합니다."""
    if not _pointer(image):
        raise CompositeLayerError("Composite 메모리를 계산할 기준 Image가 없습니다.")
    return sum(width * height * 4 for width, height in _paint_image_contract(image).tile_sizes)


def _paint_image_contract(image: Any) -> PaintImageContract:
    """Image Backend 오류를 Composite 도메인 오류로 변환해 계약을 반환합니다."""
    try:
        return image_contract(image)
    except PaintImageContractError as exc:
        raise CompositeLayerError(str(exc)) from exc


def composite_memory_estimate(settings: Any) -> int:
    """현재 Layer Stack과 동일한 Composite Image의 최소 메모리 사용량을 반환합니다."""
    image = next(
        (layer.image for layer in getattr(settings, "layers", ()) if _pointer(layer.image)),
        None,
    )
    return _tile_memory_bytes(image)


def composite_memory_label(settings: Any) -> str:
    """Composite 생성 경고에 쓸 최소 Image 메모리 안내 문구를 반환합니다."""
    memory = composite_memory_estimate(settings)
    multiplier = 10 if bool(getattr(settings, "composite_include_alpha", False)) else 1
    return f"예상 추가 메모리: 최소 {memory * multiplier / (1024.0 * 1024.0):.1f} MiB"


def composite_recompose_summary(settings: Any) -> str:
    """재합성 확인창에 표시할 삭제 범위와 평면 Source 메모리 경고를 반환합니다."""
    session = _composite_session(settings, require_active=True)
    memory = composite_memory_estimate(settings)
    return (
        f"기존 Source {len(session.sources)}개와 관련 리소스를 새 결과 검증 후 삭제합니다. "
        "현재 Composite 출력만 평면 Layer로 남으며, 삭제된 Source는 Composite 해제로 복구되지 않습니다. "
        "되돌리기는 Blender Undo 기록이 있을 때만 가능합니다. "
        f"예상 추가 메모리: 최소 {memory * (10 if bool(getattr(settings, 'composite_include_alpha', False)) else 2) / (1024.0 * 1024.0):.1f} MiB"
    )


@dataclass(frozen=True, slots=True)
class CompositeSession:
    """검증된 Composite 세션 식별자와 Composite·Source만 전달합니다."""

    session_uuid: str
    composite: Any
    sources: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class CompositeSourcePartition:
    """버튼 시점의 단순 선택과 눈 상태로 확정한 연속 Source 구간을 보관합니다."""

    normal_layer_uuids: tuple[str, ...]
    source_layer_uuids: tuple[str, ...]
    bake_included_layer_uuids: tuple[str, ...]
    source_insert_index: int


def composite_source_partition(normal_layers: tuple[Any, ...]) -> CompositeSourcePartition:
    """선택 최상단부터 최하단까지 Source로 묶고 선택·눈 ON만 Bake 대상으로 계산합니다."""
    selected_indices = tuple(
        index
        for index, layer in enumerate(normal_layers)
        if bool(getattr(layer, "ui_selected", False))
    )
    if not selected_indices:
        raise CompositeLayerError("Composite 대상 Layer를 하나 이상 선택하십시오.")
    source_start = min(selected_indices)
    source_end = max(selected_indices)
    source_layers = normal_layers[source_start : source_end + 1]
    bake_layers = tuple(
        layer
        for layer in source_layers
        if bool(getattr(layer, "ui_selected", False))
        and bool(getattr(layer, "visible", False))
    )
    if not bake_layers:
        raise CompositeLayerError("선택한 Composite 대상 중 눈이 켜진 Layer가 없습니다.")
    return CompositeSourcePartition(
        normal_layer_uuids=tuple(str(layer.layer_uuid) for layer in normal_layers),
        source_layer_uuids=tuple(str(layer.layer_uuid) for layer in source_layers),
        bake_included_layer_uuids=tuple(str(layer.layer_uuid) for layer in bake_layers),
        source_insert_index=source_start,
    )


@dataclass(frozen=True, slots=True)
class CyclesDeviceUseSnapshot:
    """Cycles 장치 하나의 복구용 식별자와 사용 상태를 보관합니다."""

    device: Any
    name: str
    device_type: str
    device_id: str
    index: int
    use: bool


@dataclass(slots=True)
class CyclesBakeDeviceState:
    """Composite Bake 중에만 바뀌는 Cycles 장치 환경의 복구 상태를 보관합니다."""

    scene_device: str
    scene_samples: int | None
    preferences: Any
    compute_device_type: str | None
    device_uses: tuple[CyclesDeviceUseSnapshot, ...]
    restored: bool = False
    restore_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceMaterialNodeState:
    """관리 Material 밖의 원본 Node Tree 활성·선택 상태를 보관합니다."""

    tree: Any
    active_node: Any
    node_selection: tuple[tuple[Any, bool], ...]


@dataclass(slots=True)
class CompositeBakePreparation:
    """Bake 중에만 유지해야 하는 임시 Shader·선택·렌더 상태를 보관합니다."""

    scene: Any
    tree: Any
    surface: Any
    surface_link: Any
    active_node: Any
    node_selection: tuple[tuple[Any, bool], ...]
    source_material_node_states: tuple[SourceMaterialNodeState, ...]
    render_engine: str
    cycles_device_state: CyclesBakeDeviceState
    device_detail: str
    selection_snapshot: Any
    view_layer: Any
    bake_node: Any
    emission: Any
    temporary_nodes: tuple[Any, ...]
    film_transparent: bool
    bake_margin_type: str
    bake_margin: int
    restored: bool = False
    restore_errors: tuple[str, ...] = ()


@dataclass(slots=True)
class CompositeCreateJob:
    """GUI·Background 양쪽이 공유하는 Composite 생성 Transaction 상태를 보관합니다."""

    scene: Any
    settings: Any
    normal_layer_uuids: tuple[str, ...]
    source_layer_uuids: tuple[str, ...]
    bake_included_layer_uuids: tuple[str, ...]
    source_insert_index: int
    memory_bytes: int
    previous_state: tuple[Any, ...]
    source_visibility: tuple[tuple[str, bool, bool, str, bool], ...]
    composite_uuid: str
    session_uuid: str
    include_alpha: bool
    kind: str = "CREATE"
    recomposite_snapshot: tuple[
        tuple[Any, str, str, str, str, int, bool, bool, bool, Any], ...
    ] = ()
    old_session: CompositeSession | None = None
    old_composite_uuid: str = ""
    old_source_layer_uuids: tuple[str, ...] = ()
    flatten_image: Any = None
    old_composite_image: Any = None
    progress: Any = None
    composite_image: Any = None
    alpha_image: Any = None
    flatten_alpha_image: Any = None
    bake_preparation: CompositeBakePreparation | None = None
    ui_bake_preparation: CompositeBakePreparation | None = None
    phase: str = "CREATION_STARTED"
    result_image: Any = None
    error_message: str = ""
    committed: bool = False
    rolled_back: bool = False
    deletion_started: bool = False
    primary_error: str = ""
    persistent_rollback_error: str = ""
    ui_restore_warning: str = ""
    native_bake_outcome: str = "PENDING"
    native_bake_stage: str = ""
    native_bake_started: bool = False
    native_bake_finished: bool = False
    native_bake_job_stopped: bool = False
    native_bake_started_at: float = 0.0
    abort_after_bake: bool = False
    reuse_old_composite_image: bool = False
    color_bake_finished: bool = False
    alpha_bake_finished: bool = False
    combine_finished: bool = False


def composite_alpha_warning(settings: Any) -> str:
    """픽셀을 읽지 않고 선택 Source의 불투명 바닥 보장 여부를 안내합니다."""
    try:
        if str(getattr(settings, "composite_state", "NONE")) == "ACTIVE":
            source_layers = _recomposite_partition(
                settings,
                _composite_session(settings, require_active=True),
            )
            source_uuids = set(source_layers.source_layer_uuids)
            bake_uuids = set(source_layers.bake_included_layer_uuids)
            layers = tuple(
                layer for layer in settings.layers if str(layer.layer_uuid) in source_uuids
            )
        else:
            partition = composite_source_partition(composite_source_layers(settings))
            source_uuids = set(partition.source_layer_uuids)
            bake_uuids = set(partition.bake_included_layer_uuids)
            layers = tuple(
                layer for layer in settings.layers if str(layer.layer_uuid) in source_uuids
            )
    except (AttributeError, CompositeLayerError, RuntimeError, TypeError, ValueError):
        return ""
    baked = tuple(layer for layer in layers if str(layer.layer_uuid) in bake_uuids)
    if not baked:
        return ""
    has_original = any(bool(getattr(layer, "is_original_layer", False)) for layer in layers)
    bottom = baked[-1]
    has_mask = any(
        str(getattr(getattr(layer, "mask", None), "mask_uuid", "") or "")
        for layer in layers
    )
    source_indices = tuple(
        index for index, layer in enumerate(settings.layers) if str(layer.layer_uuid) in source_uuids
    )
    has_lower_sibling = bool(source_indices and max(source_indices) < len(settings.layers) - 1)
    if (
        not has_original
        or not bool(getattr(bottom, "is_original_layer", False))
        or has_mask
        or has_lower_sibling
    ):
        return "Alpha를 제외하면 빈 영역이 불투명한 색상으로 합쳐질 수 있습니다."
    return ""


def _update_job_progress(
    job: CompositeCreateJob,
    completed_steps: int,
    phase: str,
    detail: str,
) -> None:
    """후처리 진행창이 등록된 경우에만 실제 완료 단계를 표시합니다."""
    if job.progress is not None:
        update_composite_progress(job.progress, completed_steps, phase, detail)


@dataclass(slots=True)
class CompositeReleaseSnapshot:
    """Composite 해제의 논리 Commit 전 실패 때 되돌릴 Scene·Node 활성 상태를 보관합니다."""

    composite_state: str
    active_layer_index: int
    overlay_image: Any
    active_paint_image: Any
    paint_mask_mode: bool
    active_mask_uuid: str
    source_visibility: tuple[tuple[Any, bool], ...]
    active_node: Any
    node_selection: tuple[tuple[Any, bool], ...]
    paint_active_slot: int


def _job_source_layers(job: CompositeCreateJob) -> tuple[Any, ...]:
    """CollectionProperty 이동 뒤에도 UUID로 현재 원본 Layer 항목을 다시 찾습니다."""
    layers = tuple(
        find_layer(job.settings, layer_uuid)
        for layer_uuid in job.source_layer_uuids
    )
    if any(layer is None for layer in layers):
        raise CompositeLayerError("Composite 원본 Layer UUID를 현재 목록에서 찾을 수 없습니다.")
    return layers


def _job_bake_layers(job: CompositeCreateJob) -> tuple[Any, ...]:
    """버튼 Snapshot에서 Bake 포함으로 확정된 현재 Source Layer만 반환합니다."""
    layers = tuple(
        find_layer(job.settings, layer_uuid)
        for layer_uuid in job.bake_included_layer_uuids
    )
    if any(layer is None for layer in layers):
        raise CompositeLayerError("Composite Bake 대상 Layer UUID를 현재 목록에서 찾을 수 없습니다.")
    return layers


def _composite_session(settings: Any, *, require_active: bool) -> CompositeSession:
    """역할·세션 UUID·Image 소유권이 일치하는 Composite와 Source를 검증합니다."""
    state = str(getattr(settings, "composite_state", "NONE"))
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
    if require_active and state != "ACTIVE":
        raise CompositeLayerError("활성 Composite 작업 Layer가 없습니다.")
    if not session_uuid:
        raise CompositeLayerError("Composite 세션 UUID가 없습니다.")
    chain = composite_chain_layers(settings)
    composites = tuple(
        layer
        for layer in chain
        if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
        == COMPOSITE_LAYER_ROLE
    )
    if len(composites) != 1:
        raise CompositeLayerError("Composite 기준 Layer가 하나가 아닙니다.")
    composite = composites[0]
    if str(getattr(settings, "composite_layer_uuid", "") or "") != str(
        composite.layer_uuid
    ):
        raise CompositeLayerError("Composite 기준 Layer UUID가 설정과 다릅니다.")
    if not _owned_layer_image(composite.image, str(composite.layer_uuid)):
        raise CompositeLayerError("Composite Image의 소유권이 유효하지 않습니다.")
    if str(composite.image.get(COMPOSITE_IMAGE_SESSION_KEY, "") or "") != session_uuid:
        raise CompositeLayerError("Composite Image 세션 UUID가 일치하지 않습니다.")
    sources = composite_source_layers(settings)
    if not sources:
        raise CompositeLayerError("Composite 복구에 필요한 원본 Layer가 없습니다.")
    if int(getattr(settings, "composite_source_schema_version", 0)) >= COMPOSITE_SOURCE_SCHEMA_VERSION:
        if not any(bool(getattr(layer, "composite_bake_included", False)) for layer in sources):
            raise CompositeLayerError("활성 Composite Source에 Bake 포함 Layer가 없습니다.")
        if any(
            str(getattr(layer, "composite_source_session_uuid", "") or "") != session_uuid
            for layer in sources
        ):
            raise CompositeLayerError("Composite Source 세션 UUID가 서로 다릅니다.")
    return CompositeSession(session_uuid, composite, sources)


def validate_composite_session(scene: Any) -> CompositeSession:
    """Composite 해제 전에 Layer·Image·최종 Shader Chain 계약을 fail-closed로 검증합니다."""
    settings = _settings(scene)
    if settings is None or not _pointer(getattr(settings, "paint_material", None)):
        raise CompositeLayerError("Composite 검증에 필요한 관리 Material이 없습니다.")
    try:
        session = _composite_session(settings, require_active=True)
        validate_active_layer_graph(scene, settings.paint_material)
    except Exception as exc:
        settings.composite_state = "STALE"
        if isinstance(exc, CompositeLayerError):
            raise
        if isinstance(exc, LayerShaderError):
            raise CompositeLayerError(f"Composite Shader Chain이 손상되었습니다: {exc}") from exc
        raise
    return session


def composite_release_summary(settings: Any) -> tuple[str, tuple[str, ...]]:
    """확인 대화상자에 표시할 Composite와 빈 호환 삭제 목록을 반환합니다."""
    session = _composite_session(settings, require_active=True)
    return str(session.composite.name), ()


def _capture_selection(context: Any) -> tuple[Any, tuple[tuple[Any, bool], ...], str]:
    """Bake 전 Object 활성·선택·Mode 상태를 복구 가능한 값으로 캡처합니다."""
    view_layer = context.view_layer
    active = view_layer.objects.active
    selected = tuple((obj, bool(obj.select_get())) for obj in view_layer.objects)
    mode = str(getattr(active, "mode", "OBJECT") or "OBJECT")
    return active, selected, mode


def _activate_bake_target(context: Any, target: Any) -> None:
    """Cycles Bake가 요구하는 Object Mode와 단일 활성 선택 Target을 준비합니다."""
    view_layer = context.view_layer
    if target.mode != "OBJECT":
        for obj in view_layer.objects:
            obj.select_set(False)
        target.select_set(True)
        view_layer.objects.active = target
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in view_layer.objects:
        obj.select_set(False)
    target.select_set(True)
    view_layer.objects.active = target


def _mode_restore_override(context: Any) -> tuple[Any, Any, Any]:
    """완료 시점 화면에서 Mode 복구에 쓸 VIEW_3D WINDOW Context를 새로 찾습니다."""
    window = getattr(context, "window", None)
    if not _pointer(window):
        raise CompositeLayerError("Texture Paint Mode 복구에 필요한 Blender Window를 찾지 못했습니다.")
    screen = getattr(window, "screen", None)
    current_area = getattr(context, "area", None)
    areas = []
    if getattr(current_area, "type", "") == "VIEW_3D":
        areas.append(current_area)
    areas.extend(
        area
        for area in getattr(screen, "areas", ())
        if getattr(area, "type", "") == "VIEW_3D" and area not in areas
    )
    for area in areas:
        region = next(
            (candidate for candidate in area.regions if candidate.type == "WINDOW"),
            None,
        )
        if region is not None:
            return window, area, region
    raise CompositeLayerError("Texture Paint Mode 복구에 필요한 VIEW_3D 영역을 찾지 못했습니다.")


def _restore_selection(
    context: Any,
    snapshot: tuple[Any, tuple[tuple[Any, bool], ...], str],
    *,
    view_layer: Any,
) -> None:
    """유효한 Modal Context에서 Bake 전 Object 선택·활성·Mode를 복구합니다."""
    active, selected, mode = snapshot
    for obj in view_layer.objects:
        obj.select_set(False)
    for obj, was_selected in selected:
        if _pointer(obj):
            obj.select_set(was_selected)
    if not _pointer(active):
        return
    view_layer.objects.active = active
    if str(getattr(active, "mode", "OBJECT")) == mode:
        return
    window, area, region = _mode_restore_override(context)
    with context.temp_override(
        window=window,
        area=area,
        region=region,
        scene=context.scene,
        view_layer=view_layer,
    ):
        if not bpy.ops.object.mode_set.poll():
            raise CompositeLayerError("Texture Paint Mode 복구 조건을 만족하지 않습니다.")
        result = bpy.ops.object.mode_set(mode=mode)
    if "FINISHED" not in result:
        raise CompositeLayerError("Texture Paint Mode 복구를 완료하지 못했습니다.")


def _cycles_preferences() -> Any | None:
    """현재 Blender 세션에서 Cycles 장치 환경설정을 안전하게 찾습니다."""
    try:
        addon = bpy.context.preferences.addons.get("cycles")
        return getattr(addon, "preferences", None)
    except (AttributeError, KeyError, ReferenceError, RuntimeError, TypeError):
        return None


def _cycles_devices(preferences: Any) -> tuple[Any, ...]:
    """Cycles가 현재 조회 가능한 CPU·GPU 장치 목록을 최신 상태로 반환합니다."""
    getter = getattr(preferences, "get_devices", None)
    if callable(getter):
        getter()
    return tuple(getattr(preferences, "devices", ()))


def _capture_cycles_bake_device_state(
    scene: Any,
    *,
    preferences: Any | None = None,
) -> CyclesBakeDeviceState:
    """Bake 전에 바뀔 Cycles 장치·Render Samples 상태를 복구용으로 캡처합니다."""
    resolved_preferences = preferences if preferences is not None else _cycles_preferences()
    cycles = getattr(scene, "cycles", None)
    scene_device = str(getattr(cycles, "device", "CPU"))
    try:
        scene_samples = int(cycles.samples)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        scene_samples = None
    if resolved_preferences is None:
        return CyclesBakeDeviceState(scene_device, scene_samples, None, None, ())
    try:
        compute_device_type = str(resolved_preferences.compute_device_type)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        compute_device_type = None
    try:
        device_uses = tuple(
            CyclesDeviceUseSnapshot(
                device=device,
                name=str(getattr(device, "name", "")),
                device_type=str(getattr(device, "type", "")),
                device_id=str(getattr(device, "id", "") or ""),
                index=index,
                use=bool(getattr(device, "use", False)),
            )
            for index, device in enumerate(_cycles_devices(resolved_preferences))
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        device_uses = ()
    return CyclesBakeDeviceState(
        scene_device,
        scene_samples,
        resolved_preferences,
        compute_device_type,
        device_uses,
    )


def _matching_cycles_device(
    current_devices: tuple[Any, ...],
    snapshot: CyclesDeviceUseSnapshot,
) -> Any | None:
    """재조회된 장치 중 스냅샷 장치와 동일한 항목을 안정적으로 찾습니다."""
    for device in current_devices:
        if _pointer(device) and _pointer(device) == _pointer(snapshot.device):
            return device
    if snapshot.device_id:
        identified = next(
            (
                device
                for device in current_devices
                if str(getattr(device, "id", "") or "") == snapshot.device_id
                and str(getattr(device, "type", "")) == snapshot.device_type
            ),
            None,
        )
        if identified is not None:
            return identified
    if 0 <= snapshot.index < len(current_devices):
        indexed = current_devices[snapshot.index]
        if (
            str(getattr(indexed, "name", "")) == snapshot.name
            and str(getattr(indexed, "type", "")) == snapshot.device_type
        ):
            return indexed
    return next(
        (
            device
            for device in current_devices
            if str(getattr(device, "name", "")) == snapshot.name
            and str(getattr(device, "type", "")) == snapshot.device_type
        ),
        None,
    )


def _restore_cycles_bake_device_state(
    scene: Any,
    state: CyclesBakeDeviceState,
) -> None:
    """Composite Bake가 바꾼 Cycles 장치 환경설정을 멱등으로 원래 값에 복구합니다."""
    if state.restored:
        if state.restore_errors:
            raise CompositeLayerError(
                "Composite Bake Cycles 장치 복구에 실패했습니다: "
                + "; ".join(state.restore_errors)
            )
        return
    errors: list[str] = []

    def restore_step(label: str, callback: Any) -> None:
        """독립적인 Cycles 복구 실패를 모아 기존 Transaction 오류 정책에 전달합니다."""
        try:
            callback()
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    preferences = state.preferences
    if preferences is not None and state.compute_device_type is not None:
        restore_step(
            "Cycles Backend",
            lambda: setattr(
                preferences,
                "compute_device_type",
                state.compute_device_type,
            ),
        )
        try:
            current_devices = _cycles_devices(preferences)
        except Exception as exc:
            current_devices = ()
            errors.append(f"Cycles 장치 재조회: {exc}")
        for snapshot in state.device_uses:
            device = _matching_cycles_device(current_devices, snapshot)
            if device is None:
                errors.append(
                    "Cycles 장치를 복구할 수 없습니다: "
                    f"{snapshot.device_type} {snapshot.name}"
                )
                continue
            restore_step(
                f"Cycles 장치 사용 상태 {snapshot.device_type} {snapshot.name}",
                lambda device=device, use=snapshot.use: setattr(device, "use", use),
            )
    if state.scene_samples is not None:
        restore_step(
            "Cycles Render Samples",
            lambda: setattr(scene.cycles, "samples", state.scene_samples),
        )
    restore_step(
        "Cycles Scene 장치",
        lambda: setattr(scene.cycles, "device", state.scene_device),
    )
    state.restore_errors = tuple(errors)
    state.restored = True
    if errors:
        raise CompositeLayerError(
            "Composite Bake Cycles 장치 복구에 실패했습니다: " + "; ".join(errors)
        )


def _set_cycles_device(scene: Any, device: str) -> bool:
    """지원되는 Cycles Scene 장치를 설정하고 실제 반영 여부를 반환합니다."""
    try:
        scene.cycles.device = str(device)
        return str(scene.cycles.device) == str(device)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _set_composite_bake_samples(scene: Any) -> None:
    """Cycles Emission Bake 직전에만 Render Samples를 고정값 1로 설정합니다."""
    try:
        scene.cycles.samples = COMPOSITE_BAKE_SAMPLES
        if int(scene.cycles.samples) != COMPOSITE_BAKE_SAMPLES:
            raise CompositeLayerError("Cycles Render Samples 값을 1로 적용하지 못했습니다.")
    except CompositeLayerError:
        raise
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        raise CompositeLayerError(
            "Composite Bake용 Cycles Render Samples를 설정하지 못했습니다."
        ) from exc


def _configure_cycles_bake_device(
    scene: Any,
    state: CyclesBakeDeviceState,
) -> str:
    """Bake 중 OPTIX 우선 GPU를 독점 선택하고 불가하면 명시적으로 CPU를 사용합니다."""
    preferences = state.preferences
    if preferences is None:
        _set_cycles_device(scene, "CPU")
        return "Cycles GPU 환경설정을 찾지 못해 CPU로 베이크합니다"
    try:
        available_types = {
            str(getattr(device, "type", ""))
            for device in _cycles_devices(preferences)
        }
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        available_types = set()
    for backend in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        if backend not in available_types:
            continue
        try:
            preferences.compute_device_type = backend
            if str(preferences.compute_device_type) != backend:
                continue
            devices = _cycles_devices(preferences)
            gpu_devices = tuple(
                device
                for device in devices
                if str(getattr(device, "type", "")) == backend
            )
            if not gpu_devices:
                continue
            for device in devices:
                device.use = False
            for device in gpu_devices:
                device.use = True
            if (
                not all(bool(getattr(device, "use", False)) for device in gpu_devices)
                or any(
                    bool(getattr(device, "use", False))
                    for device in devices
                    if str(getattr(device, "type", "")) == "CPU"
                )
                or not _set_cycles_device(scene, "GPU")
            ):
                continue
            names = ", ".join(str(getattr(device, "name", backend)) for device in gpu_devices)
            return f"Cycles {backend} GPU로 베이크합니다: {names}"
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue
    _set_cycles_device(scene, "CPU")
    return "Cycles GPU를 사용할 수 없어 CPU로 베이크합니다"


def _capture_source_material_node_states(
    target: Any,
    managed_material: Any,
) -> tuple[SourceMaterialNodeState, ...]:
    """관리 Material 외 Object 슬롯 Node Tree의 활성·선택 상태를 한 번만 보관합니다."""
    states: list[SourceMaterialNodeState] = []
    seen_trees: set[int] = set()
    managed_pointer = _pointer(managed_material)
    managed_tree_pointer = _pointer(getattr(managed_material, "node_tree", None))
    for slot in tuple(getattr(target, "material_slots", ())):
        material = getattr(slot, "material", None)
        if not _pointer(material) or _pointer(material) == managed_pointer:
            continue
        tree = getattr(material, "node_tree", None)
        tree_pointer = _pointer(tree)
        if (
            not tree_pointer
            or tree_pointer == managed_tree_pointer
            or tree_pointer in seen_trees
        ):
            continue
        seen_trees.add(tree_pointer)
        states.append(
            SourceMaterialNodeState(
                tree=tree,
                active_node=tree.nodes.active,
                node_selection=tuple(
                    (node, bool(node.select)) for node in tree.nodes
                ),
            )
        )
    return tuple(states)


def _suspend_source_material_bake_targets(
    states: tuple[SourceMaterialNodeState, ...],
) -> None:
    """원본 Material의 Image Texture가 Bake 타깃으로 선택되지 않게 잠시 비활성화합니다."""
    for state in states:
        for node, _selected in state.node_selection:
            if _pointer(node):
                node.select = False
        state.tree.nodes.active = None


def _restore_source_material_node_states(
    states: tuple[SourceMaterialNodeState, ...],
) -> None:
    """원본 Material Node Tree의 활성·선택 상태를 모두 멱등하게 복구합니다."""
    errors: list[str] = []
    for state in states:
        try:
            if not _pointer(state.tree):
                raise CompositeLayerError("원본 Material Node Tree가 삭제되었습니다.")
            for node, selected in state.node_selection:
                if not _pointer(node):
                    raise CompositeLayerError("원본 Material Node가 삭제되었습니다.")
                node.select = selected
            if state.active_node is None:
                state.tree.nodes.active = None
            elif _pointer(state.active_node):
                state.tree.nodes.active = state.active_node
            else:
                raise CompositeLayerError("원본 Material의 활성 Node가 삭제되었습니다.")
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise CompositeLayerError(
            "원본 Material Node 상태를 복구하지 못했습니다: " + "; ".join(errors)
        )


def _prepare_final_base_color_bake(
    context: Any,
    image: Any,
) -> CompositeBakePreparation:
    """Base Color Bake가 끝날 때까지 유지할 임시 Emission과 활성 Image Node를 준비합니다."""
    scene = context.scene
    settings = _settings(scene)
    material = settings.paint_material
    target = settings.paint_target
    output, principled = _active_output_and_principled(material)
    base = principled.inputs["Base Color"]
    if len(base.links) != 1:
        raise CompositeLayerError("Composite Bake 전 최종 Base Color 연결이 하나여야 합니다.")
    final_color = base.links[0].from_socket
    tree = material.node_tree
    surface = output.inputs["Surface"]
    surface_link = surface.links[0].from_socket if surface.links else None
    active_node = tree.nodes.active
    node_selection = tuple((node, bool(node.select)) for node in tree.nodes)
    source_material_node_states = _capture_source_material_node_states(
        target,
        material,
    )
    render_engine = str(scene.render.engine)
    film_transparent = bool(scene.render.film_transparent)
    bake_margin_type = str(scene.render.bake.margin_type)
    bake_margin = _composite_bake_margin(image)
    cycles_device_state = _capture_cycles_bake_device_state(scene)
    device_detail = "Cycles 장치 설정 전"
    selection_snapshot = _capture_selection(context)
    bake_node = None
    emission = None
    recovery = CompositeBakePreparation(
        scene=scene,
        tree=tree,
        surface=surface,
        surface_link=surface_link,
        active_node=active_node,
        node_selection=node_selection,
        source_material_node_states=source_material_node_states,
        render_engine=render_engine,
        cycles_device_state=cycles_device_state,
        device_detail=device_detail,
        selection_snapshot=selection_snapshot,
        view_layer=context.view_layer,
        bake_node=None,
        emission=None,
        temporary_nodes=(),
        film_transparent=film_transparent,
        bake_margin_type=bake_margin_type,
        bake_margin=bake_margin,
    )
    try:
        scene.render.bake.margin_type = "ADJACENT_FACES"
        _suspend_source_material_bake_targets(source_material_node_states)
        bake_node = tree.nodes.new("ShaderNodeTexImage")
        bake_node.name = "PP Composite Bake Target"
        bake_node.label = "Composite Bake Target"
        bake_node.image = image
        for node in tree.nodes:
            node.select = False
        bake_node.select = True
        tree.nodes.active = bake_node
        emission = tree.nodes.new("ShaderNodeEmission")
        emission.name = "PP Composite Bake Emission"
        tree.links.new(final_color, emission.inputs["Color"])
        for link in tuple(surface.links):
            tree.links.remove(link)
        tree.links.new(emission.outputs["Emission"], surface)
        scene.render.engine = "CYCLES"
        device_detail = _configure_cycles_bake_device(scene, cycles_device_state)
        _activate_bake_target(context, target)
        recovery.bake_node = bake_node
        recovery.emission = emission
        recovery.device_detail = device_detail
        return recovery
    except Exception:
        recovery.bake_node = bake_node
        recovery.emission = emission
        _restore_bake_persistent_state(recovery)
        try:
            _restore_bake_ui_state(context, recovery)
        except Exception:
            pass
        raise


def _layer_mix_node(tree: Any, layer: Any) -> Any:
    """지정 Layer의 단일 관리 Mix Node를 Bake용 Source Shader에서 찾습니다."""
    layer_uuid = str(layer.layer_uuid)
    matches = tuple(
        node
        for node in tree.nodes
        if str(node.get(RECORD_KIND_KEY, "") or "") == LAYER_MIX_NODE_ROLE
        and str(node.get(LAYER_UUID_KEY, "") or "") == layer_uuid
    )
    if len(matches) != 1:
        raise CompositeLayerError("부분 Composite Bake에 필요한 Layer Mix Node를 하나로 찾을 수 없습니다.")
    return matches[0]


def _connect_layer_bake_factor(tree: Any, layer_mix: Any, shader_mix: Any) -> None:
    """기존 Layer Alpha·Mask Factor 연결을 임시 Shader Mix Factor에 그대로 복사합니다."""
    factor = layer_mix.inputs[0]
    if len(factor.links) > 1:
        raise CompositeLayerError("부분 Composite Bake Layer Factor 연결이 중복되었습니다.")
    if factor.links:
        tree.links.new(factor.links[0].from_socket, shader_mix.inputs[0])
    else:
        shader_mix.inputs[0].default_value = float(factor.default_value)


def _combined_image_source_sockets(
    tree: Any,
    image: Any,
    layer_uuid: str,
) -> tuple[Any, Any, tuple[Any, ...]]:
    """평면 Combined RGBA Image를 임시 Source Shader용 직선 색·Alpha Socket으로 변환합니다."""
    if not _pointer(image):
        raise CompositeLayerError("재합성 평면 Source Image가 유효하지 않습니다.")
    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.name = f"PP Composite Flatten Source {layer_uuid}"
    image_node.label = "Composite Flatten Source"
    image_node.image = image
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.name = f"PP Composite Flatten Separate {layer_uuid}"
    separate.mode = "RGB"
    alpha = tree.nodes.new("ShaderNodeMath")
    alpha.name = f"PP Composite Flatten Alpha {layer_uuid}"
    alpha.operation = "MAXIMUM"
    alpha.inputs[0].default_value = 0.000001
    combine = tree.nodes.new("ShaderNodeCombineColor")
    combine.name = f"PP Composite Flatten Combine {layer_uuid}"
    combine.mode = "RGB"
    divide_nodes = tuple(tree.nodes.new("ShaderNodeMath") for _index in range(3))
    tree.links.new(image_node.outputs["Color"], separate.inputs["Color"])
    tree.links.new(image_node.outputs["Alpha"], alpha.inputs[1])
    for channel, divide in zip(("Red", "Green", "Blue"), divide_nodes):
        divide.name = f"PP Composite Flatten Unpremultiply {channel} {layer_uuid}"
        divide.operation = "DIVIDE"
        tree.links.new(separate.outputs[channel], divide.inputs[0])
        tree.links.new(alpha.outputs[0], divide.inputs[1])
        tree.links.new(divide.outputs[0], combine.inputs[channel])
    return (
        combine.outputs["Color"],
        image_node.outputs["Alpha"],
        (image_node, separate, alpha, combine, *divide_nodes),
    )


def _prepare_source_rgba_bake(
    context: Any,
    image: Any,
    job: CompositeCreateJob,
    *,
    source_image_overrides: dict[str, Any] | None = None,
) -> CompositeBakePreparation:
    """선택 Source와 필요 시 평면 Source Override를 Combined RGBA Bake용 Surface로 준비합니다."""
    scene = context.scene
    settings = _settings(scene)
    material = settings.paint_material
    target = settings.paint_target
    output, _principled_node = _active_output_and_principled(material)
    tree = material.node_tree
    surface = output.inputs["Surface"]
    surface_link = surface.links[0].from_socket if surface.links else None
    active_node = tree.nodes.active
    node_selection = tuple((node, bool(node.select)) for node in tree.nodes)
    source_material_node_states = _capture_source_material_node_states(target, material)
    render_engine = str(scene.render.engine)
    bake_margin_type = str(scene.render.bake.margin_type)
    bake_margin = _composite_bake_margin(image)
    cycles_device_state = _capture_cycles_bake_device_state(scene)
    selection_snapshot = _capture_selection(context)
    bake_node = None
    temporary_nodes: list[Any] = []
    recovery = CompositeBakePreparation(
        scene=scene,
        tree=tree,
        surface=surface,
        surface_link=surface_link,
        active_node=active_node,
        node_selection=node_selection,
        source_material_node_states=source_material_node_states,
        render_engine=render_engine,
        cycles_device_state=cycles_device_state,
        device_detail="Cycles 장치 설정 전",
        selection_snapshot=selection_snapshot,
        view_layer=context.view_layer,
        bake_node=None,
        emission=None,
        temporary_nodes=(),
        film_transparent=bool(scene.render.film_transparent),
        bake_margin_type=bake_margin_type,
        bake_margin=bake_margin,
    )
    try:
        scene.render.bake.margin_type = "ADJACENT_FACES"
        _suspend_source_material_bake_targets(source_material_node_states)
        image.alpha_mode = "STRAIGHT"
        bake_node = tree.nodes.new("ShaderNodeTexImage")
        bake_node.name = "PP Composite Source RGBA Bake Target"
        bake_node.label = "Composite Source RGBA Bake Target"
        bake_node.image = image
        for node in tree.nodes:
            node.select = False
        bake_node.select = True
        tree.nodes.active = bake_node
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        transparent.name = "PP Composite Source Transparent"
        temporary_nodes.append(transparent)
        current_shader = transparent.outputs[0]
        top_emission = None
        for layer in reversed(_job_bake_layers(job)):
            layer_uuid = str(layer.layer_uuid)
            layer_mix = _layer_mix_node(tree, layer)
            override_image = (source_image_overrides or {}).get(layer_uuid)
            if _pointer(override_image):
                color_socket, factor_socket, override_nodes = _combined_image_source_sockets(
                    tree,
                    override_image,
                    layer_uuid,
                )
                temporary_nodes.extend(override_nodes)
            else:
                color_input = layer_mix.inputs[2]
                if len(color_input.links) != 1:
                    raise CompositeLayerError("부분 Composite Bake Layer 색상 연결이 유효하지 않습니다.")
                color_socket = color_input.links[0].from_socket
                factor_socket = None
            emission = tree.nodes.new("ShaderNodeEmission")
            emission.name = f"PP Composite Source Emission {layer.layer_uuid}"
            emission.label = str(layer.name)
            shader_mix = tree.nodes.new("ShaderNodeMixShader")
            shader_mix.name = f"PP Composite Source Mix {layer.layer_uuid}"
            shader_mix.label = str(layer.name)
            tree.links.new(color_socket, emission.inputs["Color"])
            if factor_socket is None:
                _connect_layer_bake_factor(tree, layer_mix, shader_mix)
            else:
                tree.links.new(factor_socket, shader_mix.inputs[0])
            tree.links.new(current_shader, shader_mix.inputs[1])
            tree.links.new(emission.outputs["Emission"], shader_mix.inputs[2])
            current_shader = shader_mix.outputs[0]
            temporary_nodes.extend((emission, shader_mix))
            top_emission = emission
        for link in tuple(surface.links):
            tree.links.remove(link)
        tree.links.new(current_shader, surface)
        scene.render.engine = "CYCLES"
        scene.render.film_transparent = True
        recovery.device_detail = _configure_cycles_bake_device(scene, cycles_device_state)
        _activate_bake_target(context, target)
        recovery.bake_node = bake_node
        recovery.emission = top_emission
        recovery.temporary_nodes = tuple(temporary_nodes)
        return recovery
    except Exception:
        recovery.bake_node = bake_node
        recovery.temporary_nodes = tuple(temporary_nodes)
        _restore_bake_persistent_state(recovery)
        try:
            _restore_bake_ui_state(context, recovery)
        except Exception:
            pass
        raise


def _prepare_source_emit_bake(
    context: Any,
    image: Any,
    job: Any,
    *,
    channel: str,
    source_image_overrides: dict[str, Any] | None = None,
) -> CompositeBakePreparation:
    """선택 Source의 premultiplied Color 또는 coverage Alpha를 EMIT Surface로 준비합니다."""
    if channel not in {"COLOR", "ALPHA"}:
        raise CompositeLayerError("Composite EMIT 채널이 유효하지 않습니다.")
    scene = context.scene
    settings = _settings(scene)
    material = settings.paint_material
    target = settings.paint_target
    output, _principled_node = _active_output_and_principled(material)
    tree = material.node_tree
    surface = output.inputs["Surface"]
    surface_link = surface.links[0].from_socket if surface.links else None
    active_node = tree.nodes.active
    node_selection = tuple((node, bool(node.select)) for node in tree.nodes)
    source_material_node_states = _capture_source_material_node_states(target, material)
    recovery = CompositeBakePreparation(
        scene=scene,
        tree=tree,
        surface=surface,
        surface_link=surface_link,
        active_node=active_node,
        node_selection=node_selection,
        source_material_node_states=source_material_node_states,
        render_engine=str(scene.render.engine),
        cycles_device_state=_capture_cycles_bake_device_state(scene),
        device_detail="Cycles 장치 설정 전",
        selection_snapshot=_capture_selection(context),
        view_layer=context.view_layer,
        bake_node=None,
        emission=None,
        temporary_nodes=(),
        film_transparent=bool(scene.render.film_transparent),
        bake_margin_type=str(scene.render.bake.margin_type),
        bake_margin=_composite_bake_margin(image),
    )
    bake_node = None
    temporary_nodes: list[Any] = []
    try:
        scene.render.bake.margin_type = "ADJACENT_FACES"
        _suspend_source_material_bake_targets(source_material_node_states)
        image.alpha_mode = "STRAIGHT"
        bake_node = tree.nodes.new("ShaderNodeTexImage")
        bake_node.name = f"PP Composite {channel.title()} EMIT Bake Target"
        bake_node.label = f"Composite {channel.title()} Bake Target"
        bake_node.image = image
        for node in tree.nodes:
            node.select = False
        bake_node.select = True
        tree.nodes.active = bake_node

        base = tree.nodes.new("ShaderNodeRGB")
        base.name = f"PP Composite {channel.title()} Base"
        base.outputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
        temporary_nodes.append(base)
        current_socket = base.outputs[0]
        for layer in reversed(_job_bake_layers(job)):
            layer_uuid = str(layer.layer_uuid)
            layer_mix = _layer_mix_node(tree, layer)
            override_image = (source_image_overrides or {}).get(layer_uuid)
            if _pointer(override_image):
                color_socket, factor_socket, override_nodes = _combined_image_source_sockets(
                    tree,
                    override_image,
                    layer_uuid,
                )
                temporary_nodes.extend(override_nodes)
            else:
                color_input = layer_mix.inputs[2]
                if len(color_input.links) != 1:
                    raise CompositeLayerError("부분 Composite EMIT Layer 색상 연결이 유효하지 않습니다.")
                color_socket = color_input.links[0].from_socket
                factor_socket = None
            mix = tree.nodes.new("ShaderNodeMixRGB")
            mix.name = f"PP Composite {channel.title()} Mix {layer_uuid}"
            mix.blend_type = "MIX"
            tree.links.new(current_socket, mix.inputs[1])
            if channel == "COLOR":
                tree.links.new(color_socket, mix.inputs[2])
            else:
                mix.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)
            if factor_socket is None:
                factor = layer_mix.inputs[0]
                if factor.links:
                    tree.links.new(factor.links[0].from_socket, mix.inputs[0])
                else:
                    mix.inputs[0].default_value = float(factor.default_value)
            else:
                tree.links.new(factor_socket, mix.inputs[0])
            current_socket = mix.outputs[0]
            temporary_nodes.append(mix)

        emission = tree.nodes.new("ShaderNodeEmission")
        emission.name = f"PP Composite {channel.title()} Bake Emission"
        tree.links.new(current_socket, emission.inputs["Color"])
        for link in tuple(surface.links):
            tree.links.remove(link)
        tree.links.new(emission.outputs["Emission"], surface)
        scene.render.engine = "CYCLES"
        scene.render.film_transparent = True
        recovery.device_detail = _configure_cycles_bake_device(
            scene,
            recovery.cycles_device_state,
        )
        _activate_bake_target(context, target)
        recovery.bake_node = bake_node
        recovery.emission = emission
        recovery.temporary_nodes = tuple(temporary_nodes)
        return recovery
    except Exception:
        recovery.bake_node = bake_node
        recovery.temporary_nodes = tuple(temporary_nodes)
        _restore_bake_persistent_state(recovery)
        try:
            _restore_bake_ui_state(context, recovery)
        except Exception:
            pass
        raise


def combine_color_alpha_numpy(color_image: Any, alpha_image: Any) -> None:
    """같은 Paint Image 계약의 Alpha R을 Color A에 버퍼 단위로 결합합니다."""
    if _native_bake_is_running():
        raise CompositeLayerError("OBJECT_BAKE Job 종료 전에는 NumPy 결합을 실행할 수 없습니다.")
    color_contract = _paint_image_contract(color_image)
    alpha_contract = _paint_image_contract(alpha_image)
    if color_contract != alpha_contract:
        raise CompositeLayerError("Color와 Alpha Image의 백엔드·버퍼 계약이 다릅니다.")
    color_active = int(getattr(color_image.tiles, "active_index", 0))
    alpha_active = int(getattr(alpha_image.tiles, "active_index", 0))
    try:
        for tile_index, tile_number in enumerate(color_contract.tile_numbers):
            if color_contract.backend != SINGLE_BACKEND:
                color_image.tiles.active_index = tile_index
                alpha_image.tiles.active_index = tile_index
            color_size = len(color_image.pixels)
            alpha_size = len(alpha_image.pixels)
            if color_size != alpha_size or color_size % 4:
                raise CompositeLayerError(
                    f"논리 버퍼 {tile_number}의 Color·Alpha Pixel 크기가 다릅니다."
                )
            color_pixels = np.empty(color_size, dtype=np.float32)
            alpha_pixels = np.empty(alpha_size, dtype=np.float32)
            color_image.pixels.foreach_get(color_pixels)
            alpha_image.pixels.foreach_get(alpha_pixels)
            color_pixels[3::4] = alpha_pixels[0::4]
            color_image.pixels.foreach_set(color_pixels)
            del color_pixels
            del alpha_pixels
        color_image.update()
    except MemoryError as exc:
        raise CompositeLayerError("Composite Alpha 결합용 NumPy 메모리를 할당하지 못했습니다.") from exc
    finally:
        if color_contract.backend != SINGLE_BACKEND:
            color_image.tiles.active_index = color_active
            alpha_image.tiles.active_index = alpha_active


def _restore_bake_persistent_state(preparation: CompositeBakePreparation) -> None:
    """임시 Bake Surface·Node·렌더 상태를 영구 Transaction 계약에 맞춰 복구합니다."""
    if preparation.restored:
        if preparation.restore_errors:
            raise CompositeLayerError(
                "Composite Bake 임시 상태 복구에 실패했습니다: "
                + "; ".join(preparation.restore_errors)
            )
        return
    errors: list[str] = []

    def restore_step(label: str, callback: Any) -> None:
        """복구 단계 하나의 오류를 기록하고 다음 독립 복구를 계속 실행합니다."""
        try:
            callback()
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    def restore_surface() -> None:
        """Bake용 Emission Surface 연결을 제거하고 원래 Surface 연결을 되돌립니다."""
        for link in tuple(preparation.surface.links):
            preparation.tree.links.remove(link)
        if preparation.surface_link is not None:
            preparation.tree.links.new(preparation.surface_link, preparation.surface)

    def remove_emission() -> None:
        """임시 Emission Node를 남기지 않고 제거합니다."""
        if preparation.emission is not None and _pointer(preparation.emission):
            preparation.tree.nodes.remove(preparation.emission)

    def remove_bake_node() -> None:
        """Bake 전용 Image Texture Node를 남기지 않고 제거합니다."""
        if preparation.bake_node is not None and _pointer(preparation.bake_node):
            preparation.tree.nodes.remove(preparation.bake_node)

    def remove_temporary_nodes() -> None:
        """부분 RGBA Bake 전용 Shader Node를 모두 남기지 않고 제거합니다."""
        retained = {_pointer(preparation.bake_node), _pointer(preparation.emission)}
        for node in preparation.temporary_nodes:
            if _pointer(node) and _pointer(node) not in retained:
                preparation.tree.nodes.remove(node)

    def restore_node_selection() -> None:
        """Bake 전 Shader Node 선택 상태와 활성 Node를 복구합니다."""
        for node, selected in preparation.node_selection:
            if _pointer(node):
                node.select = selected
        if _pointer(preparation.active_node):
            preparation.tree.nodes.active = preparation.active_node

    def restore_source_material_nodes() -> None:
        """Bake 타깃에서 제외한 원본 Material Node 상태를 다시 복구합니다."""
        _restore_source_material_node_states(preparation.source_material_node_states)

    restore_step("Surface 연결", restore_surface)
    restore_step("Emission Node", remove_emission)
    restore_step("Bake Image Node", remove_bake_node)
    restore_step("부분 Bake Shader Node", remove_temporary_nodes)
    restore_step("Node 선택", restore_node_selection)
    restore_step("원본 Material Node 선택", restore_source_material_nodes)
    restore_step(
        "Cycles 장치",
        lambda: _restore_cycles_bake_device_state(
            preparation.scene,
            preparation.cycles_device_state,
        ),
    )
    restore_step(
        "Render Engine",
        lambda: setattr(preparation.scene.render, "engine", preparation.render_engine),
    )
    restore_step(
        "Film Transparent",
        lambda: setattr(
            preparation.scene.render,
            "film_transparent",
            preparation.film_transparent,
        ),
    )
    restore_step(
        "Bake Margin Type",
        lambda: setattr(
            preparation.scene.render.bake,
            "margin_type",
            preparation.bake_margin_type,
        ),
    )
    preparation.restore_errors = tuple(errors)
    preparation.restored = True
    if errors:
        raise CompositeLayerError(
            "Composite Bake 임시 상태 복구에 실패했습니다: " + "; ".join(errors)
        )


def _restore_bake_ui_state(context: Any, preparation: CompositeBakePreparation) -> None:
    """현재 Modal Context에서 선택과 원래 Object Mode만 별도 복구합니다."""
    if context is None:
        raise CompositeLayerError("선택과 Texture Paint Mode 복구에 쓸 Modal Context가 없습니다.")
    if _pointer(getattr(context, "scene", None)) != _pointer(preparation.scene):
        raise CompositeLayerError("Composite Bake와 다른 Scene에서는 선택과 Mode를 복구할 수 없습니다.")
    if _pointer(getattr(context, "view_layer", None)) != _pointer(preparation.view_layer):
        raise CompositeLayerError("Composite Bake와 다른 View Layer에서는 선택과 Mode를 복구할 수 없습니다.")
    _restore_selection(
        context,
        preparation.selection_snapshot,
        view_layer=preparation.view_layer,
    )


def prepare_emit_bake(
    context: Any,
    image: Any,
    settings: Any,
    bake_included_layer_uuids: tuple[str, ...],
    *,
    channel: str,
    source_image_overrides: dict[str, Any] | None = None,
) -> CompositeBakePreparation:
    """Job 종류와 무관하게 선택 Layer의 Native·동기 EMIT Bake 상태를 준비합니다."""
    preparation = _prepare_source_emit_bake(
        context,
        image,
        SimpleNamespace(
            settings=settings,
            bake_included_layer_uuids=tuple(
                str(layer_uuid) for layer_uuid in bake_included_layer_uuids
            ),
        ),
        channel=str(channel),
        source_image_overrides=source_image_overrides,
    )
    try:
        _set_composite_bake_samples(preparation.scene)
    except Exception:
        _restore_bake_persistent_state(preparation)
        _restore_bake_ui_state(context, preparation)
        raise
    return preparation


def invoke_prepared_emit_bake(
    preparation: CompositeBakePreparation,
    *,
    execution_mode: str,
) -> set[str]:
    """준비된 EMIT Bake를 지정한 Blender 실행 모드로 호출하고 반환 계약을 검증합니다."""
    mode = str(execution_mode)
    if mode not in {"EXEC_DEFAULT", "INVOKE_DEFAULT"}:
        raise CompositeLayerError("지원하지 않는 EMIT Bake 실행 모드입니다.")
    result = bpy.ops.object.bake(
        mode,
        type="EMIT",
        target="IMAGE_TEXTURES",
        use_clear=True,
        margin=preparation.bake_margin,
    )
    expected = "FINISHED" if mode == "EXEC_DEFAULT" else "RUNNING_MODAL"
    if expected not in result:
        raise CompositeLayerError(
            "Cycles EMIT Bake를 완료하지 못했습니다."
            if mode == "EXEC_DEFAULT"
            else "Cycles Native EMIT Bake Job을 시작하지 못했습니다."
        )
    return set(result)


def restore_emit_bake_persistent_state(
    preparation: CompositeBakePreparation,
) -> None:
    """Job 종류와 무관하게 EMIT Bake의 Node·렌더·장치 상태를 멱등 복구합니다."""
    _restore_bake_persistent_state(preparation)


def restore_emit_bake_ui_state(
    context: Any,
    preparation: CompositeBakePreparation,
) -> None:
    """Job 종류와 무관하게 EMIT Bake 전 선택·Mode 상태를 현재 Context에서 복구합니다."""
    _restore_bake_ui_state(context, preparation)


def _bake_final_base_color(
    context: Any,
    image: Any,
    *,
    job: CompositeCreateJob | None = None,
) -> None:
    """Background와 직접 호출 경로에서 현재 Base Color를 동기 Cycles Emission Bake로 기록합니다."""
    if job is not None and job.phase != "IMAGE_PREPARED":
        raise CompositeLayerError("Composite Bake 시작 단계가 유효하지 않습니다.")
    preparation = _prepare_final_base_color_bake(context, image)
    if job is not None:
        job.bake_preparation = preparation
        _update_job_progress(
            job,
            2,
            "Baking",
            preparation.device_detail,
        )
    try:
        _set_composite_bake_samples(preparation.scene)
        result = bpy.ops.object.bake(
            "EXEC_DEFAULT",
            type="EMIT",
            target="IMAGE_TEXTURES",
            use_clear=True,
            margin=preparation.bake_margin,
        )
        if "FINISHED" not in result:
            raise CompositeLayerError("Cycles Emission Bake가 완료되지 않았습니다.")
        image.update()
    finally:
        _restore_bake_persistent_state(preparation)
        if job is None:
            _restore_bake_ui_state(context, preparation)
    if job is not None:
        job.phase = "BAKED"


def _bake_source_rgba(context: Any, image: Any, *, job: CompositeCreateJob) -> None:
    """선택 Source를 순차 Color·Alpha EMIT와 NumPy 결합으로 기록합니다."""
    if job.phase != "IMAGE_PREPARED":
        raise CompositeLayerError("부분 Composite Bake 시작 단계가 유효하지 않습니다.")
    preparation = _prepare_source_emit_bake(
        context,
        image,
        job,
        channel="COLOR",
        source_image_overrides=_recomposite_flatten_source_overrides(job),
    )
    job.bake_preparation = preparation
    job.ui_bake_preparation = preparation
    _update_job_progress(job, 2, "Baking", preparation.device_detail)
    try:
        _set_composite_bake_samples(preparation.scene)
        result = bpy.ops.object.bake(
            "EXEC_DEFAULT",
            type="EMIT",
            target="IMAGE_TEXTURES",
            use_clear=True,
            margin=preparation.bake_margin,
        )
        if "FINISHED" not in result:
            raise CompositeLayerError("Cycles Color EMIT Bake가 완료되지 않았습니다.")
        image.update()
    finally:
        _restore_bake_persistent_state(preparation)
    job.bake_preparation = None
    job.color_bake_finished = True
    if job.include_alpha:
        alpha_preparation = _prepare_source_emit_bake(
            context,
            job.alpha_image,
            job,
            channel="ALPHA",
            source_image_overrides=_recomposite_flatten_source_overrides(job),
        )
        job.bake_preparation = alpha_preparation
        try:
            _set_composite_bake_samples(alpha_preparation.scene)
            result = bpy.ops.object.bake(
                "EXEC_DEFAULT",
                type="EMIT",
                target="IMAGE_TEXTURES",
                use_clear=True,
                margin=alpha_preparation.bake_margin,
            )
            if "FINISHED" not in result:
                raise CompositeLayerError("Cycles Alpha EMIT Bake가 완료되지 않았습니다.")
            job.alpha_image.update()
        finally:
            _restore_bake_persistent_state(alpha_preparation)
        job.bake_preparation = None
        job.alpha_bake_finished = True
        combine_color_alpha_numpy(image, job.alpha_image)
        job.combine_finished = True
        _remove_image(job.alpha_image)
        job.alpha_image = None
    job.phase = "BAKED"


def _bake_texture_export_channel(
    context: Any,
    image: Any,
    job: Any,
    *,
    channel: str,
) -> None:
    """Texture Export의 premultiplied Color 또는 coverage Alpha를 동기 EMIT Bake합니다."""
    preparation = prepare_emit_bake(
        context,
        image,
        job.settings,
        tuple(job.bake_included_layer_uuids),
        channel=channel,
    )
    try:
        invoke_prepared_emit_bake(preparation, execution_mode="EXEC_DEFAULT")
        image.update()
    finally:
        restore_emit_bake_persistent_state(preparation)
        restore_emit_bake_ui_state(context, preparation)


def _validated_texture_export_layers(
    context: Any,
    visible_layers: tuple[Any, ...] | None,
) -> tuple[Any, tuple[Any, ...]]:
    """Texture Export용 가시 Layer가 현재 Base Color Graph의 순서와 일치하는지 검증합니다."""
    scene = context.scene
    settings = _settings(scene)
    if settings is None:
        raise CompositeLayerError("최종 Texture Export에 필요한 설정이 없습니다.")
    try:
        validate_active_layer_graph(scene, settings.paint_material)
        graph_layers = active_base_color_graph_layers(settings)
    except LayerShaderError as exc:
        raise CompositeLayerError(f"최종 Texture Export Layer Graph가 유효하지 않습니다: {exc}") from exc
    selected_layers = (
        tuple(visible_layers)
        if visible_layers is not None
        else tuple(layer for layer in graph_layers if bool(getattr(layer, "visible", False)))
    )
    graph_pointers = {_pointer(layer) for layer in graph_layers}
    selected_pointers = tuple(_pointer(layer) for layer in selected_layers)
    if not selected_layers:
        raise CompositeLayerError("내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.")
    if len(set(selected_pointers)) != len(selected_layers):
        raise CompositeLayerError("최종 Texture Export Layer가 중복되었습니다.")
    if any(
        pointer not in graph_pointers or not bool(getattr(layer, "visible", False))
        for pointer, layer in zip(selected_pointers, selected_layers)
    ):
        raise CompositeLayerError("최종 Texture Export Layer가 현재 가시 Graph와 다릅니다.")
    selected_pointer_set = set(selected_pointers)
    expected_order = tuple(
        _pointer(layer) for layer in graph_layers if _pointer(layer) in selected_pointer_set
    )
    if selected_pointers != expected_order:
        raise CompositeLayerError("최종 Texture Export Layer 순서가 현재 Graph와 다릅니다.")
    return settings, selected_layers


def create_texture_export_bake_images(
    context: Any,
    visible_layers: tuple[Any, ...] | None = None,
) -> tuple[Any, Any, tuple[str, ...]]:
    """GUI·동기 Export가 공유할 Color·Alpha 임시 Image와 고정 Layer UUID를 생성합니다."""
    settings, selected_layers = _validated_texture_export_layers(context, visible_layers)
    image = create_composite_layer_image(context.scene, new_uuid())
    alpha_image = None
    try:
        alpha_image = create_composite_layer_image(context.scene, new_uuid())
        alpha_image[COMBINED_RGBA_IMAGE_KEY] = False
        alpha_image.colorspace_settings.name = "Non-Color"
        return (
            image,
            alpha_image,
            tuple(str(layer.layer_uuid) for layer in selected_layers),
        )
    except Exception:
        if alpha_image is not None and _pointer(alpha_image):
            _remove_image(alpha_image)
        _remove_image(image)
        raise


def create_final_texture_export_image(
    context: Any,
    visible_layers: tuple[Any, ...] | None = None,
) -> Any:
    """명시된 가시 Base Color Graph를 같은 백엔드의 임시 RGBA Image에 Bake합니다."""
    settings, selected_layers = _validated_texture_export_layers(context, visible_layers)
    image, alpha_image, layer_uuids = create_texture_export_bake_images(
        context,
        selected_layers,
    )
    succeeded = False
    try:
        temporary_job = SimpleNamespace(
            settings=settings,
            bake_included_layer_uuids=layer_uuids,
        )
        _bake_texture_export_channel(
            context,
            image,
            temporary_job,
            channel="COLOR",
        )
        _bake_texture_export_channel(
            context,
            alpha_image,
            temporary_job,
            channel="ALPHA",
        )
        combine_color_alpha_numpy(image, alpha_image)
        _remove_image(alpha_image)
        alpha_image = None
        succeeded = True
        return image
    finally:
        if alpha_image is not None and _pointer(alpha_image):
            _remove_image(alpha_image)
        if not succeeded:
            _remove_image(image)


def remove_final_texture_export_image(image: Any) -> None:
    """최종 Texture Export 전용 임시 Image와 내구성 등록을 안전하게 제거합니다."""
    _remove_image(image)


def native_object_bake_running() -> bool:
    """현재 Blender가 공통 Native OBJECT_BAKE Job을 실행 중인지 반환합니다."""
    try:
        return bool(bpy.app.is_job_running("OBJECT_BAKE"))
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _native_bake_is_running() -> bool:
    """기존 Composite 내부 API에서 공통 OBJECT_BAKE 실행 여부를 반환합니다."""
    return native_object_bake_running()


def _native_bake_image(job: CompositeCreateJob, stage: str) -> Any:
    """Native Bake 단계가 기록할 현재 Image를 검증해 반환합니다."""
    if stage == "FLATTEN_COLOR":
        image = job.flatten_image
    elif stage == "FLATTEN_ALPHA":
        image = job.flatten_alpha_image
    elif stage == "COLOR":
        image = job.composite_image
    elif stage == "ALPHA":
        image = job.alpha_image
    else:
        raise CompositeLayerError("Composite Native Bake 단계가 유효하지 않습니다.")
    if not _pointer(image):
        raise CompositeLayerError("Composite Native Bake 대상 Image가 없습니다.")
    return image


def _native_bake_progress_label(job: CompositeCreateJob, stage: str) -> tuple[str, str]:
    """현재 Native Bake 단계에 맞는 읽기 전용 Overlay 문구를 반환합니다."""
    if stage.startswith("FLATTEN_"):
        channel = "Color" if stage.endswith("COLOR") else "Alpha"
        return (
            f"{channel} Flatten Baking",
            f"기존 Composite의 {channel}를 EMIT로 계산하고 Blender Image로 변환 중",
        )
    total = 2 if job.include_alpha else 1
    if stage == "ALPHA":
        return ("Alpha Baking 2/2", "Alpha coverage를 EMIT로 계산하고 Blender Image로 변환 중")
    return (
        f"Color Baking 1/{total}",
        "Composite Color를 EMIT로 계산하고 Blender Image로 변환 중",
    )


def mark_native_bake_job_stopped(job: CompositeCreateJob) -> None:
    """Native Job 종료를 기록하고 완료된 이미지 변환 단계를 Overlay에 표시합니다."""
    if job.phase != "NATIVE_BAKING" or not job.native_bake_stage:
        raise CompositeLayerError("종료 상태를 기록할 Composite Native Bake가 없습니다.")
    if job.native_bake_job_stopped:
        return
    job.native_bake_job_stopped = True
    if job.native_bake_outcome != "COMPLETE" or job.progress is None:
        return
    stage = str(job.native_bake_stage)
    end_composite_native_bake(job.progress)
    if stage.startswith("FLATTEN_"):
        _update_job_progress(
            job,
            2,
            "Flatten Image Ready",
            "평면 Source EMIT 계산과 Blender 이미지 변환이 완료되었습니다",
        )
        return
    sequence = (
        "2/2"
        if stage == "ALPHA"
        else f"1/{2 if bool(getattr(job, 'include_alpha', False)) else 1}"
    )
    _update_job_progress(
        job,
        3,
        f"Image Ready {sequence}",
        f"{sequence} Cycles 계산과 Blender 이미지 변환이 완료되었습니다",
    )


def prepare_native_bake(
    context: Any,
    job: CompositeCreateJob,
    stage: str,
) -> None:
    """Native Bake 시작 전 임시 Surface·Cycles 설정·읽기 전용 진행 표시를 준비합니다."""
    if stage == "COMPOSITE":
        stage = "COLOR"
    elif stage == "FLATTEN":
        stage = "FLATTEN_COLOR"
    if job.phase != "IMAGE_PREPARED":
        raise CompositeLayerError("Composite Native Bake 준비 단계가 유효하지 않습니다.")
    if job.bake_preparation is not None or job.native_bake_stage:
        raise CompositeLayerError("이미 준비되었거나 실행 중인 Composite Native Bake가 있습니다.")
    image = _native_bake_image(job, stage)
    if stage.startswith("FLATTEN_"):
        if job.reuse_old_composite_image:
            raise CompositeLayerError("재사용 Composite Image에는 평면화 Bake를 실행하지 않습니다.")
        temporary_job = SimpleNamespace(
            settings=job.settings,
            bake_included_layer_uuids=(str(job.old_composite_uuid),),
        )
        preparation = prepare_emit_bake(
            context,
            image,
            temporary_job.settings,
            tuple(temporary_job.bake_included_layer_uuids),
            channel="COLOR" if stage.endswith("COLOR") else "ALPHA",
        )
    elif stage in {"COLOR", "ALPHA"}:
        preparation = prepare_emit_bake(
            context,
            image,
            job.settings,
            tuple(job.bake_included_layer_uuids),
            channel=stage,
            source_image_overrides=_recomposite_flatten_source_overrides(job),
        )
    else:
        raise CompositeLayerError("Composite Native Bake 단계가 유효하지 않습니다.")
    try:
        job.bake_preparation = preparation
        if job.ui_bake_preparation is None:
            job.ui_bake_preparation = preparation
        job.native_bake_stage = str(stage)
        job.native_bake_outcome = "PENDING"
        job.native_bake_started = False
        job.native_bake_finished = False
        job.native_bake_job_stopped = False
        job.native_bake_started_at = monotonic()
        job.phase = "NATIVE_BAKING"
        if job.progress is not None:
            phase, detail = _native_bake_progress_label(job, stage)
            begin_composite_native_bake(
                job.progress,
                phase=phase,
                detail=detail,
                device_detail=preparation.device_detail,
                image=image,
                sample_count=COMPOSITE_BAKE_SAMPLES,
            )
    except Exception:
        restore_emit_bake_persistent_state(preparation)
        raise


def invoke_native_bake(context: Any, job: CompositeCreateJob) -> None:
    """준비된 Composite Surface에 INVOKE_DEFAULT Native Bake Job을 시작합니다."""
    if job.phase != "NATIVE_BAKING" or not job.native_bake_stage:
        raise CompositeLayerError("시작할 준비가 된 Composite Native Bake가 없습니다.")
    preparation = job.bake_preparation
    if preparation is None:
        raise CompositeLayerError("Composite Native Bake Margin을 계산할 준비 상태가 없습니다.")
    invoke_prepared_emit_bake(preparation, execution_mode="INVOKE_DEFAULT")
    job.native_bake_started = True


def mark_native_bake_outcome(
    scene_or_target: Any,
    target_or_outcome: Any,
    outcome: str | None = None,
) -> None:
    """Bake Handler의 Blender 5.2 Object 인자 또는 호환 Scene·Object 신호만 기록합니다."""
    if outcome is None:
        scene = None
        target = scene_or_target
        outcome = str(target_or_outcome)
    elif target_or_outcome is None:
        scene = None
        target = scene_or_target
    else:
        scene = scene_or_target
        target = target_or_outcome
    if outcome not in {"RUNNING", "COMPLETE", "CANCELLED"}:
        return
    for job in tuple(_CREATE_JOBS.values()):
        if (
            job.phase != "NATIVE_BAKING"
            or not job.native_bake_stage
            or (scene is not None and _pointer(job.scene) != _pointer(scene))
            or _pointer(getattr(job.settings, "paint_target", None)) != _pointer(target)
        ):
            continue
        if outcome == "RUNNING":
            if job.native_bake_outcome == "PENDING":
                job.native_bake_outcome = "RUNNING"
            return
        if job.native_bake_outcome in {"COMPLETE", "CANCELLED"}:
            return
        job.native_bake_outcome = outcome
        job.native_bake_finished = True
        return


def finalize_native_bake(context: Any, job: CompositeCreateJob) -> str:
    """종료된 Native Job의 Image와 임시 상태만 다음 Timer 틱에서 확정합니다."""
    if _native_bake_is_running():
        raise CompositeLayerError("Native Bake Job이 끝나기 전에 결과를 처리할 수 없습니다.")
    if job.phase != "NATIVE_BAKING" or job.native_bake_outcome != "COMPLETE":
        raise CompositeLayerError("완료 신호가 확인된 Composite Native Bake가 없습니다.")
    stage = str(job.native_bake_stage)
    image = _native_bake_image(job, stage)
    preparation = job.bake_preparation
    if preparation is None:
        raise CompositeLayerError("Composite Native Bake 복구 상태가 없습니다.")
    restore_emit_bake_persistent_state(preparation)
    image.update()
    job.bake_preparation = None
    job.native_bake_stage = ""
    job.native_bake_finished = True
    if job.progress is not None:
        end_composite_native_bake(job.progress)
    if stage.startswith("FLATTEN_"):
        job.phase = "IMAGE_PREPARED"
        _update_job_progress(
            job,
            2,
            "Flatten Complete",
            "기존 Composite 평면 Source Image를 검증했습니다",
        )
        return stage
    if stage == "COLOR":
        job.color_bake_finished = True
    elif stage == "ALPHA":
        job.alpha_bake_finished = True
    job.phase = "IMAGE_PREPARED"
    _update_job_progress(
        job,
        3,
        "Bake Complete",
        "새 Composite Bake 결과 Image를 검증합니다",
    )
    return stage


def advance_composite_native_sequence(
    context: Any,
    job: CompositeCreateJob,
    completed_stage: str,
) -> None:
    """안전 틱에서 Color·Alpha·NumPy 순차 단계를 다음 상태로 전환합니다."""
    if _native_bake_is_running():
        raise CompositeLayerError("OBJECT_BAKE Job 종료 전에는 다음 Composite 단계를 시작할 수 없습니다.")
    if completed_stage == "FLATTEN_COLOR":
        prepare_native_bake(context, job, "FLATTEN_ALPHA")
        invoke_native_bake(context, job)
        return
    if completed_stage == "FLATTEN_ALPHA":
        combine_color_alpha_numpy(job.flatten_image, job.flatten_alpha_image)
        _remove_image(job.flatten_alpha_image)
        job.flatten_alpha_image = None
        prepare_native_bake(context, job, "COLOR")
        invoke_native_bake(context, job)
        return
    if completed_stage == "COLOR" and job.include_alpha:
        prepare_native_bake(context, job, "ALPHA")
        invoke_native_bake(context, job)
        return
    if completed_stage == "ALPHA":
        _update_job_progress(job, 2, "Color + Alpha 결합 중", "Paint Image 버퍼별 NumPy 채널 결합을 실행합니다")
        combine_color_alpha_numpy(job.composite_image, job.alpha_image)
        job.combine_finished = True
        _remove_image(job.alpha_image)
        job.alpha_image = None
    job.phase = "BAKED"
    _complete_composite_bake(context, job)


def rollback_native_bake(
    context: Any | None,
    job: CompositeCreateJob,
    error: Exception,
) -> None:
    """취소·누락 신호 뒤 Native Job 종료를 확인한 다음 Transaction을 롤백합니다."""
    if _native_bake_is_running():
        raise CompositeLayerError("Native Bake Job이 끝나기 전에 롤백할 수 없습니다.")
    _rollback_composite_creation(job, error, context=context)


def _clear_composite_settings(settings: Any) -> None:
    """완전히 해제된 Composite 세션의 영구 Metadata만 초기 상태로 비웁니다."""
    settings.composite_state = "NONE"
    settings.active_composite_session_uuid = ""
    settings.composite_source_schema_version = 0
    settings.composite_layer_uuid = ""
    settings.composite_saved_active_layer_uuid = ""


def _remove_created_composite(scene: Any, layer_uuid: str, image: Any) -> None:
    """생성 Transaction 실패 시 아직 확정되지 않은 Composite Layer와 Image를 정리합니다."""
    settings = _settings(scene)
    material = getattr(settings, "paint_material", None)
    index = next(
        (
            position
            for position, layer in enumerate(settings.layers)
            if str(layer.layer_uuid) == str(layer_uuid)
        ),
        -1,
    )
    if index >= 0:
        settings.layers.remove(index)
    if material is not None and material.node_tree is not None and _pointer(image):
        remove_owned_layer_bundles(material, (str(layer_uuid),), (image,))


def _normal_layers_for_composite_creation(settings: Any) -> tuple[Any, ...]:
    """Composite 생성 전에 변경 없이 대상 상태와 원본 Layer Shader Chain을 검증합니다."""
    if settings is None:
        raise CompositeLayerError("Projection Painting 설정이 없습니다.")
    if composite_session_is_active(settings) or str(settings.composite_state) in {
        "CREATING",
        "RECOMPOSITING",
        "RELEASING",
    }:
        raise CompositeLayerError("이미 Composite 작업 Layer가 활성 또는 처리 중입니다.")
    if str(settings.composite_state) == "STALE":
        raise CompositeLayerError("손상된 Composite 상태는 자동으로 덮어쓰지 않습니다.")
    if settings.paint_material is None or settings.paint_target is None:
        raise CompositeLayerError("Composite 생성에 필요한 관리 Material과 Paint Target이 없습니다.")
    if bool(settings.paint_mask_mode) or str(settings.active_mask_uuid or ""):
        raise CompositeLayerError("Mask 편집을 끝내고 Layer를 선택한 뒤 Composite를 생성하십시오.")
    if (
        str(settings.layer_preview_uuid or "")
        or str(settings.mask_preview_adjustment_uuid or "")
    ):
        raise CompositeLayerError("단독 미리보기를 해제한 뒤 Composite를 생성하십시오.")
    normal_layers = composite_source_layers(settings)
    if not normal_layers:
        raise CompositeLayerError("Composite로 만들 Paint Layer가 없습니다.")
    try:
        validate_layer_graph(
            settings.paint_material,
            tuple(str(layer.layer_uuid) for layer in normal_layers),
            expected_original_base=False,
        )
    except LayerShaderError as exc:
        raise CompositeLayerError(f"Composite 생성 전 Layer Shader Chain이 유효하지 않습니다: {exc}") from exc
    return normal_layers


def _recomposite_partition(
    settings: Any,
    session: CompositeSession,
) -> CompositeSourcePartition:
    """표시된 형제 Layer와 현재 Composite의 선택 범위로 재합성 Source를 계산합니다."""
    session_uuid = str(session.session_uuid)
    top_level = tuple(
        (index, layer)
        for index, layer in enumerate(settings.layers)
        if (
            str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) == NORMAL_LAYER_ROLE
            and str(getattr(layer, "composite_source_session_uuid", "") or "")
            != session_uuid
        )
        or (
            str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
            == COMPOSITE_LAYER_ROLE
            and str(getattr(layer, "composite_session_uuid", "") or "")
            == session_uuid
        )
    )
    composite_position = next(
        (
            position
            for position, (_index, layer) in enumerate(top_level)
            if str(layer.layer_uuid) == str(session.composite.layer_uuid)
        ),
        -1,
    )
    if composite_position < 0 or not bool(session.composite.ui_selected):
        raise CompositeLayerError("Composite 다시 생성에는 현재 Composite Layer를 선택해야 합니다.")
    if not bool(session.composite.visible):
        raise CompositeLayerError("Composite 다시 생성에는 현재 Composite Layer의 눈이 켜져 있어야 합니다.")
    selected_positions = tuple(
        position
        for position, (_index, layer) in enumerate(top_level)
        if bool(getattr(layer, "ui_selected", False))
    )
    if not selected_positions:
        raise CompositeLayerError("Composite 다시 생성 대상 Layer를 하나 이상 선택하십시오.")
    source_start = min(selected_positions)
    source_end = max(selected_positions)
    source_entries = top_level[source_start : source_end + 1]
    source_layers = tuple(layer for _index, layer in source_entries)
    if sum(
        str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) == COMPOSITE_LAYER_ROLE
        for layer in source_layers
    ) != 1:
        raise CompositeLayerError("Composite 다시 생성 범위에는 현재 Composite 하나만 포함해야 합니다.")
    bake_layers = tuple(
        layer
        for layer in source_layers
        if bool(getattr(layer, "ui_selected", False)) and bool(layer.visible)
    )
    if not bake_layers:
        raise CompositeLayerError("선택한 Composite 다시 생성 대상 중 눈이 켜진 Layer가 없습니다.")
    return CompositeSourcePartition(
        normal_layer_uuids=tuple(str(layer.layer_uuid) for _index, layer in top_level),
        source_layer_uuids=tuple(str(layer.layer_uuid) for layer in source_layers),
        bake_included_layer_uuids=tuple(str(layer.layer_uuid) for layer in bake_layers),
        source_insert_index=int(source_entries[0][0]),
    )


def _capture_recomposite_snapshot(
    settings: Any,
) -> tuple[tuple[Any, str, str, str, int, bool, bool, bool, Any], ...]:
    """재합성 실패 시 기존 Composite를 완전히 되돌릴 Layer 영구 상태를 보관합니다."""
    return tuple(
        (
            layer,
            str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)),
            str(getattr(layer, "composite_session_uuid", "") or ""),
            str(getattr(layer, "composite_source_session_uuid", "") or ""),
            int(getattr(layer, "composite_generation", 0)),
            bool(layer.visible),
            bool(getattr(layer, "composite_saved_visible", False)),
            bool(getattr(layer, "composite_bake_included", False)),
            layer.image,
        )
        for layer in settings.layers
    )


def _restore_recomposite_snapshot(job: CompositeCreateJob) -> None:
    """재합성 Commit 전 실패 뒤 기존 Layer 역할·Image·세션 상태를 복구합니다."""
    for (
        layer,
        role,
        composite_session_uuid,
        source_session_uuid,
        generation,
        visible,
        saved_visible,
        bake_included,
        image,
    ) in job.recomposite_snapshot:
        if _pointer(layer):
            layer.layer_role = role
            layer.composite_session_uuid = composite_session_uuid
            layer.composite_source_session_uuid = source_session_uuid
            layer.composite_generation = generation
            layer.visible = visible
            layer.composite_saved_visible = saved_visible
            layer.composite_bake_included = bake_included
            layer.image = image
    _clear_recomposite_temporary_image_markers(job.old_composite_image)


def _image_tile_contract(image: Any) -> PaintImageContract | None:
    """Image의 백엔드·논리 타일·해상도 계약을 비교 가능한 값으로 반환합니다."""
    if not _pointer(image):
        return None
    try:
        return image_contract(image)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError, PaintImageContractError):
        return None


def can_reuse_composite_image(job: CompositeCreateJob) -> bool:
    """활성 Composite Image를 평면화 Bake 없이 읽기 전용 Source로 써도 되는지 판정합니다."""
    if (
        job.kind != "RECOMPOSITE"
        or job.old_session is None
        or str(job.previous_state[0] if job.previous_state else "") != "ACTIVE"
    ):
        return False
    old_composite = job.old_session.composite
    old_image = getattr(old_composite, "image", None)
    if (
        not _owned_layer_image(old_image, str(getattr(old_composite, "layer_uuid", "") or ""))
        or _pointer(old_image) != _pointer(job.old_composite_image)
        or str(old_image.get(COMPOSITE_IMAGE_SESSION_KEY, "") or "")
        != str(job.old_session.session_uuid)
    ):
        return False
    if str(getattr(old_composite.mask, "mask_uuid", "") or ""):
        return False
    if len(tuple(getattr(old_composite, "adjustments", ()))):
        return False
    expected_image = job.composite_image
    return bool(
        _image_tile_contract(old_image)
        and _image_tile_contract(old_image) == _image_tile_contract(expected_image)
    )


def _create_recomposite_flatten_image(job: CompositeCreateJob) -> Any:
    """재합성의 두 Bake 경로에만 필요한 새 평면 Source Image를 생성하고 표식합니다."""
    if job.old_session is None:
        raise CompositeLayerError("재합성 평면 Source Image에 필요한 기존 세션이 없습니다.")
    old_composite = job.old_session.composite
    flatten_image = create_composite_layer_image(
        job.scene,
        str(old_composite.layer_uuid),
    )
    job.flatten_image = flatten_image
    _mark_recomposite_temporary_image(flatten_image, job, "FLATTEN_SOURCE")
    flatten_alpha_image = create_composite_layer_image(job.scene, new_uuid())
    flatten_alpha_image[COMBINED_RGBA_IMAGE_KEY] = False
    flatten_alpha_image.colorspace_settings.name = "Non-Color"
    job.flatten_alpha_image = flatten_alpha_image
    _mark_recomposite_temporary_image(flatten_alpha_image, job, "FLATTEN_ALPHA")
    return flatten_image


def _recomposite_flatten_source_overrides(job: CompositeCreateJob) -> dict[str, Any] | None:
    """두 Bake 재합성의 최종 Bake가 평면 Source만 읽도록 고정된 Image Override를 반환합니다."""
    if (
        job.kind != "RECOMPOSITE"
        or job.reuse_old_composite_image
        or not str(job.old_composite_uuid)
        or not _pointer(job.flatten_image)
    ):
        return None
    return {str(job.old_composite_uuid): job.flatten_image}


def prepare_recomposite_native_source(job: CompositeCreateJob) -> None:
    """GUI Native Bake 전 재사용 판정을 고정하고 필요할 때만 평면 Source Image를 준비합니다."""
    if job.kind != "RECOMPOSITE" or job.old_session is None:
        return
    if job.phase != "IMAGE_PREPARED":
        raise CompositeLayerError("재합성 Native Bake Source 준비 단계가 유효하지 않습니다.")
    job.reuse_old_composite_image = can_reuse_composite_image(job)
    if job.reuse_old_composite_image:
        job.flatten_image = job.old_composite_image
        return
    _create_recomposite_flatten_image(job)


def _start_recomposite_layer_creation(context: Any) -> CompositeCreateJob:
    """기존 ACTIVE Composite를 유지한 채 새 평면 Composite 생성 Transaction을 시작합니다."""
    scene = context.scene
    settings = _settings(scene)
    if settings is None or str(getattr(settings, "composite_state", "NONE")) != "ACTIVE":
        raise CompositeLayerError("Composite 다시 생성을 시작할 활성 Composite가 없습니다.")
    session = validate_composite_session(scene)
    if bool(settings.paint_mask_mode) or str(settings.active_mask_uuid or ""):
        raise CompositeLayerError("Mask 편집을 끝내고 Layer를 선택한 뒤 Composite를 다시 생성하십시오.")
    if str(settings.layer_preview_uuid or "") or str(settings.mask_preview_adjustment_uuid or ""):
        raise CompositeLayerError("단독 미리보기를 해제한 뒤 Composite를 다시 생성하십시오.")
    partition = _recomposite_partition(settings, session)
    previous_state = (
        str(settings.composite_state),
        str(settings.active_composite_session_uuid),
        int(settings.composite_source_schema_version),
        str(settings.composite_layer_uuid),
        str(settings.composite_saved_active_layer_uuid),
        int(settings.active_layer_index),
        settings.overlay_image,
        settings.active_paint_image,
        bool(settings.paint_mask_mode),
        str(settings.active_mask_uuid),
    )
    job = CompositeCreateJob(
        scene=scene,
        settings=settings,
        normal_layer_uuids=partition.normal_layer_uuids,
        source_layer_uuids=partition.source_layer_uuids,
        bake_included_layer_uuids=partition.bake_included_layer_uuids,
        source_insert_index=partition.source_insert_index,
        memory_bytes=composite_memory_estimate(settings) * (
            10 if bool(settings.composite_include_alpha) else 2
        ),
        previous_state=previous_state,
        source_visibility=(),
        composite_uuid=new_uuid(),
        session_uuid=new_uuid(),
        include_alpha=bool(settings.composite_include_alpha),
        kind="RECOMPOSITE",
        recomposite_snapshot=_capture_recomposite_snapshot(settings),
        old_session=session,
        old_composite_uuid=str(session.composite.layer_uuid),
        old_source_layer_uuids=tuple(str(layer.layer_uuid) for layer in session.sources),
        old_composite_image=session.composite.image,
    )
    settings.composite_state = "RECOMPOSITING"
    _CREATE_JOBS[job.session_uuid] = job
    return job


def _mark_recomposite_cleanup_stale(job: CompositeCreateJob, exc: Exception) -> None:
    """실제 삭제 시작 뒤 오류가 나면 삭제 전 상태로 위장하지 않고 STALE로 고정합니다."""
    job.rolled_back = True
    job.primary_error = str(exc)
    job.error_message = f"재합성 삭제 정리 실패: {exc}"
    job.phase = "RESULT"
    for image in (job.composite_image, job.flatten_image, job.old_composite_image):
        _clear_recomposite_temporary_image_markers(image)
    job.settings.composite_state = "STALE"
    _set_status(
        job.settings,
        "Composite 다시 생성의 기존 자원 삭제 중 오류가 발생했습니다. "
        "자동 복구하지 않았으므로 파일을 저장하지 말고 상태를 점검하십시오.",
        "ERROR",
    )


def _rollback_composite_creation(
    job: CompositeCreateJob,
    exc: Exception,
    *,
    context: Any | None = None,
) -> None:
    """생성 중단 뒤 영구 상태를 복구하고 UI 복구 경고를 별도로 보관합니다."""
    if job.rolled_back:
        return
    if job.kind == "RECOMPOSITE" and job.deletion_started:
        _mark_recomposite_cleanup_stale(job, exc)
        return
    job.rolled_back = True
    settings = job.settings
    errors: list[str] = []
    preparation: CompositeBakePreparation | None = None

    def rollback_step(label: str, callback: Any) -> None:
        """롤백 항목 하나가 실패해도 이후 독립 복구를 계속 실행합니다."""
        try:
            callback()
        except Exception as rollback_error:
            errors.append(f"{label}: {rollback_error}")

    def restore_source_state(
        layer_uuid: str,
        previous_saved: bool,
        previous_visible: bool,
        previous_source_session_uuid: str,
        previous_bake_included: bool,
    ) -> None:
        """UUID로 현재 Source Layer의 가시성·세션·Bake Metadata를 롤백합니다."""
        layer = find_layer(settings, layer_uuid)
        if layer is None:
            raise CompositeLayerError("Composite 원본 Layer UUID를 현재 목록에서 찾을 수 없습니다.")
        layer.composite_saved_visible = previous_saved
        layer.visible = previous_visible
        layer.composite_source_session_uuid = previous_source_session_uuid
        layer.composite_bake_included = previous_bake_included

    def remove_unreferenced_temporary_image(image: Any) -> None:
        """Scene·Clone 복구 뒤 참조가 사라진 재합성 임시 Image만 다시 정리합니다."""
        if _pointer(image) and int(getattr(image, "users", 0)) == 0:
            _remove_image(image)

    def protect_reconstruction_temporary_image(image: Any) -> None:
        """첫 Graph 복구 중 이전 Node가 참조하는 임시 Image를 고아 검사에서 잠시 제외합니다."""
        if _pointer(image):
            image["pp_recomposite_pending_delete"] = True

    def finalize_reconstruction_temporary_image(image: Any) -> None:
        """Graph 복구 뒤 재합성 임시 표식을 지우고 더는 참조되지 않은 Image를 삭제합니다."""
        _clear_recomposite_temporary_image_markers(image)
        remove_unreferenced_temporary_image(image)

    if job.bake_preparation is not None:
        preparation = job.bake_preparation
        job.bake_preparation = None
        rollback_step("Bake 임시 상태", lambda: _restore_bake_persistent_state(preparation))
    for label, temporary_image in (
        ("Alpha 임시 Image 제거", job.alpha_image),
        ("평면 Alpha 임시 Image 제거", job.flatten_alpha_image),
    ):
        if _pointer(temporary_image):
            rollback_step(label, lambda image=temporary_image: _remove_image(image))
    job.alpha_image = None
    job.flatten_alpha_image = None
    if _pointer(job.composite_image):
        rollback_step(
            "Composite 자원 제거",
            lambda: _remove_created_composite(
                job.scene,
                job.composite_uuid,
                job.composite_image,
            ),
        )
    if job.kind == "RECOMPOSITE" and job.recomposite_snapshot:
        rollback_step("기존 Composite Layer 상태", lambda: _restore_recomposite_snapshot(job))
    if (
        job.kind == "RECOMPOSITE"
        and not job.reuse_old_composite_image
        and _pointer(job.flatten_image)
    ):
        rollback_step(
            "평면 Source 임시 Image 제거",
            lambda: _remove_image(job.flatten_image),
        )
    for (
        layer_uuid,
        previous_saved,
        previous_visible,
        previous_source_session_uuid,
        previous_bake_included,
    ) in job.source_visibility:
        rollback_step(
            f"원본 Layer 상태 {layer_uuid}",
            lambda layer_uuid=layer_uuid, previous_saved=previous_saved, previous_visible=previous_visible, previous_source_session_uuid=previous_source_session_uuid, previous_bake_included=previous_bake_included: restore_source_state(
                layer_uuid,
                previous_saved,
                previous_visible,
                previous_source_session_uuid,
                previous_bake_included,
            ),
        )
    for attribute, value in zip(
        (
            "composite_state",
            "active_composite_session_uuid",
            "composite_source_schema_version",
            "composite_layer_uuid",
            "composite_saved_active_layer_uuid",
            "active_layer_index",
            "overlay_image",
            "active_paint_image",
            "paint_mask_mode",
            "active_mask_uuid",
        ),
        job.previous_state,
    ):
        rollback_step(
            f"Scene 상태 {attribute}",
            lambda attribute=attribute, value=value: setattr(settings, attribute, value),
        )
    if job.kind == "RECOMPOSITE":
        rollback_step(
            "Graph 복구 전 재합성 임시 Image 보호",
            lambda: tuple(
                protect_reconstruction_temporary_image(image)
                for image in (job.composite_image, job.flatten_image)
            ),
        )
    rollback_step("원본 Layer Shader Chain", lambda: reconcile_layer_graph(job.scene))
    if job.kind == "RECOMPOSITE":
        rollback_step(
            "Graph 복구 뒤 재합성 임시 Image 최종 정리",
            lambda: tuple(
                finalize_reconstruction_temporary_image(image)
                for image in (job.composite_image, job.flatten_image, job.old_composite_image)
            ),
        )
    ui_preparation = job.ui_bake_preparation or preparation
    if ui_preparation is not None and context is not None:
        try:
            _restore_bake_ui_state(context, ui_preparation)
        except Exception as ui_error:
            job.ui_restore_warning = f"UI 복구 경고: {ui_error}"
    if errors:
        try:
            settings.composite_state = "STALE"
        except Exception as state_error:
            errors.append(f"STALE 상태 기록: {state_error}")
        job.persistent_rollback_error = "; ".join(errors)
    job.phase = "RESULT"
    job.primary_error = str(exc)
    job.error_message = f"주 오류: {job.primary_error}"
    if job.persistent_rollback_error:
        job.error_message += f" / 영구 Rollback 오류: {job.persistent_rollback_error}"
    if job.ui_restore_warning:
        job.error_message += f" / {job.ui_restore_warning}"
    try:
        _set_status(settings, f"Composite Layer 생성 실패: {job.error_message}", "ERROR")
    except Exception as status_error:
        job.error_message = f"{job.error_message} / 상태 기록 실패: {status_error}"
    if job.progress is not None:
        try:
            _update_job_progress(
                job,
                int(job.progress.completed_steps),
                "Failed",
                "Composite 생성 Transaction을 롤백했습니다",
            )
        except Exception:
            pass


def start_composite_layer_creation(context: Any) -> CompositeCreateJob:
    """진행창 없이 생성 Snapshot을 보관하고 동기 GPU Bake 준비를 시작합니다."""
    if _CREATE_JOBS:
        raise CompositeLayerError("동시에 실행할 수 있는 Composite 생성 Job은 하나뿐입니다.")
    scene = context.scene
    settings = _settings(scene)
    from .overlay_migration import repair_managed_overlay_if_needed

    repair_managed_overlay_if_needed(context, overlay=settings.overlay_image)
    if str(getattr(settings, "composite_state", "NONE")) == "ACTIVE":
        return _start_recomposite_layer_creation(context)
    normal_layers = _normal_layers_for_composite_creation(settings)
    partition = composite_source_partition(normal_layers)
    memory_bytes = composite_memory_estimate(settings) * (
        10 if bool(settings.composite_include_alpha) else 1
    )
    previous_state = (
        str(settings.composite_state),
        str(settings.active_composite_session_uuid),
        int(settings.composite_source_schema_version),
        str(settings.composite_layer_uuid),
        str(settings.composite_saved_active_layer_uuid),
        int(settings.active_layer_index),
        settings.overlay_image,
        settings.active_paint_image,
        bool(settings.paint_mask_mode),
        str(settings.active_mask_uuid),
    )
    source_visibility = tuple(
        (
            str(layer.layer_uuid),
            bool(layer.composite_saved_visible),
            bool(layer.visible),
            str(getattr(layer, "composite_source_session_uuid", "") or ""),
            bool(getattr(layer, "composite_bake_included", False)),
        )
        for layer in normal_layers
        if str(layer.layer_uuid) in set(partition.source_layer_uuids)
    )
    job = CompositeCreateJob(
        scene=scene,
        settings=settings,
        normal_layer_uuids=partition.normal_layer_uuids,
        source_layer_uuids=partition.source_layer_uuids,
        bake_included_layer_uuids=partition.bake_included_layer_uuids,
        source_insert_index=partition.source_insert_index,
        memory_bytes=memory_bytes,
        previous_state=previous_state,
        source_visibility=source_visibility,
        composite_uuid=new_uuid(),
        session_uuid=new_uuid(),
        include_alpha=bool(settings.composite_include_alpha),
    )
    try:
        settings.composite_state = "CREATING"
        settings.active_composite_session_uuid = job.session_uuid
        settings.composite_source_schema_version = COMPOSITE_SOURCE_SCHEMA_VERSION
        settings.composite_saved_active_layer_uuid = str(
            settings.layers[int(settings.active_layer_index)].layer_uuid
            if 0 <= int(settings.active_layer_index) < len(settings.layers)
            else partition.normal_layer_uuids[0]
        )
        for layer in _job_source_layers(job):
            layer.composite_saved_visible = bool(layer.visible)
            layer.composite_source_session_uuid = job.session_uuid
            layer.composite_bake_included = str(layer.layer_uuid) in set(
                job.bake_included_layer_uuids
            )
        _CREATE_JOBS[job.session_uuid] = job
        return job
    except Exception as exc:
        _rollback_composite_creation(job, exc, context=context)
        raise


def prepare_composite_image(job: CompositeCreateJob) -> None:
    """GPU Bake 전에 백엔드별 Composite Image와 최초 Pack을 동기로 준비합니다."""
    if job.phase != "CREATION_STARTED":
        raise CompositeLayerError("Composite Image 생성 단계가 유효하지 않습니다.")
    try:
        job.composite_image = create_composite_layer_image(job.scene, job.composite_uuid)
        job.composite_image[COMPOSITE_IMAGE_SESSION_KEY] = job.session_uuid
        if job.include_alpha:
            job.alpha_image = create_composite_layer_image(job.scene, new_uuid())
            job.alpha_image[COMPOSITE_IMAGE_SESSION_KEY] = job.session_uuid
            job.alpha_image[COMBINED_RGBA_IMAGE_KEY] = False
            job.alpha_image.colorspace_settings.name = "Non-Color"
        if job.kind == "RECOMPOSITE":
            _mark_recomposite_temporary_image(job.composite_image, job, "NEW_COMPOSITE")
        job.phase = "IMAGE_PREPARED"
    except Exception as exc:
        _rollback_composite_creation(job, exc)


def start_composite_native_progress(
    context: Any,
    job: CompositeCreateJob,
) -> None:
    """GUI 확인 직후 Image 준비부터 표시할 읽기 전용 Composite Overlay를 시작합니다."""
    if job.progress is not None:
        raise CompositeLayerError("Composite Native Bake 진행 Overlay가 이미 등록되어 있습니다.")
    progress = create_composite_progress(context, job.memory_bytes)
    try:
        job.progress = progress
        _update_job_progress(
            job,
            0,
            "Preparing",
            "Composite Image와 Native Bake Surface를 준비 중입니다",
        )
    except Exception:
        job.progress = None
        release_composite_progress(progress)
        raise


def prepare_recomposite_flatten_image(context: Any, job: CompositeCreateJob) -> None:
    """기존 Composite 출력만 포함한 직선 Alpha 평면 Source Image를 준비합니다."""
    if job.kind != "RECOMPOSITE" or job.old_session is None:
        return
    if job.phase != "IMAGE_PREPARED":
        raise CompositeLayerError("재합성 평면 Source Image 준비 단계가 유효하지 않습니다.")
    old_composite = job.old_session.composite
    try:
        job.reuse_old_composite_image = can_reuse_composite_image(job)
        if job.reuse_old_composite_image:
            job.flatten_image = job.old_composite_image
            return
        flatten_image = _create_recomposite_flatten_image(job)
        temporary_job = SimpleNamespace(
            settings=job.settings,
            bake_included_layer_uuids=(str(old_composite.layer_uuid),),
        )
        preparation = _prepare_source_emit_bake(
            context,
            flatten_image,
            temporary_job,
            channel="COLOR",
        )
        try:
            _set_composite_bake_samples(preparation.scene)
            result = bpy.ops.object.bake(
                "EXEC_DEFAULT",
                type="EMIT",
                target="IMAGE_TEXTURES",
                use_clear=True,
                margin=preparation.bake_margin,
            )
            if "FINISHED" not in result:
                raise CompositeLayerError("기존 Composite 평면 Color EMIT Bake가 완료되지 않았습니다.")
            flatten_image.update()
        finally:
            _restore_bake_persistent_state(preparation)
        alpha_preparation = _prepare_source_emit_bake(
            context,
            job.flatten_alpha_image,
            temporary_job,
            channel="ALPHA",
        )
        try:
            _set_composite_bake_samples(alpha_preparation.scene)
            result = bpy.ops.object.bake(
                "EXEC_DEFAULT",
                type="EMIT",
                target="IMAGE_TEXTURES",
                use_clear=True,
                margin=alpha_preparation.bake_margin,
            )
            if "FINISHED" not in result:
                raise CompositeLayerError("기존 Composite 평면 Alpha EMIT Bake가 완료되지 않았습니다.")
            job.flatten_alpha_image.update()
        finally:
            _restore_bake_persistent_state(alpha_preparation)
            _restore_bake_ui_state(context, preparation)
        combine_color_alpha_numpy(flatten_image, job.flatten_alpha_image)
        _remove_image(job.flatten_alpha_image)
        job.flatten_alpha_image = None
    except Exception as exc:
        if _pointer(getattr(job, "flatten_image", None)):
            _remove_image(job.flatten_image)
            _clear_recomposite_temporary_image_markers(job.flatten_image)
            job.flatten_image = None
        _rollback_composite_creation(job, exc, context=context)
        raise


def start_composite_post_bake_progress(
    context: Any,
    job: CompositeCreateJob,
) -> None:
    """동기 Bake 성공 뒤에만 Composite 후처리 Overlay를 등록합니다."""
    if job.phase != "BAKED":
        raise CompositeLayerError("Composite 후처리 진행창 시작 단계가 유효하지 않습니다.")
    if job.progress is not None:
        raise CompositeLayerError("Composite 후처리 진행창이 이미 등록되어 있습니다.")
    progress = create_composite_progress(context, job.memory_bytes)
    try:
        job.progress = progress
        _update_job_progress(
            job,
            3,
            "Bake Complete",
            "Cycles Base Color Bake가 완료되었습니다",
        )
        job.phase = "POST_BAKE_READY"
    except Exception:
        job.progress = None
        release_composite_progress(progress)
        raise


def _set_recomposite_source_metadata(job: CompositeCreateJob, new_layer: Any) -> None:
    """새 Composite 검증 전에 명시 선택 Source의 세션 관계만 전환합니다."""
    if job.old_session is None or not _pointer(job.flatten_image):
        raise CompositeLayerError("재합성에 필요한 기존 Composite 평면 Source가 없습니다.")
    settings = job.settings
    old_composite = find_layer(settings, job.old_composite_uuid)
    if old_composite is None:
        raise CompositeLayerError("재합성할 기존 Composite Layer UUID를 찾을 수 없습니다.")
    old_generation = max(1, int(getattr(old_composite, "composite_generation", 0)))
    new_source_uuids = set(job.source_layer_uuids)
    new_bake_uuids = set(job.bake_included_layer_uuids)
    old_source_uuids = set(job.old_source_layer_uuids)
    for layer in settings.layers:
        layer_uuid = str(layer.layer_uuid)
        if layer_uuid in old_source_uuids:
            layer.composite_source_session_uuid = job.session_uuid
            layer.composite_bake_included = False
            layer.visible = False
            continue
        if layer_uuid in new_source_uuids:
            layer.composite_saved_visible = bool(layer.visible)
            layer.composite_source_session_uuid = job.session_uuid
            layer.composite_bake_included = layer_uuid in new_bake_uuids
            layer.visible = False
            continue
    old_composite.layer_role = NORMAL_LAYER_ROLE
    old_composite.composite_session_uuid = ""
    old_composite.composite_generation = old_generation
    if _pointer(job.old_composite_image) and not job.reuse_old_composite_image:
        job.old_composite_image["pp_recomposite_pending_delete"] = True
    if job.reuse_old_composite_image:
        job.old_composite_image[COMBINED_RGBA_IMAGE_KEY] = True
    old_composite.image = job.flatten_image
    old_composite.visible = False
    new_layer.composite_generation = old_generation + 1


def _remove_recomposited_composite_children(job: CompositeCreateJob) -> None:
    """검증된 재합성 뒤 과거 Source와 명시 선택 일반 Source를 Bake 후 소비합니다."""
    if job.old_session is None:
        raise CompositeLayerError("삭제할 기존 Composite 세션이 없습니다.")
    settings = job.settings
    old_composite = find_layer(settings, job.old_composite_uuid)
    if old_composite is None:
        raise CompositeLayerError("삭제할 기존 Composite Layer UUID를 찾을 수 없습니다.")
    source_uuids = tuple(
        dict.fromkeys(
            (
                *job.old_source_layer_uuids,
                *(
                    layer_uuid
                    for layer_uuid in job.source_layer_uuids
                    if layer_uuid != job.old_composite_uuid
                ),
            )
        )
    )
    source_layers = tuple(
        find_layer(settings, layer_uuid) for layer_uuid in source_uuids
    )
    if any(layer is None for layer in source_layers):
        raise CompositeLayerError("삭제할 기존 Composite Source Layer UUID를 찾을 수 없습니다.")
    source_images = _layer_bundle_images(source_layers)
    job.deletion_started = True
    for index in range(len(old_composite.adjustments) - 1, -1, -1):
        adjustment = old_composite.adjustments[index]
        remove_adjustment_resources(job.scene, old_composite, adjustment)
        old_composite.adjustments.remove(index)
    if str(old_composite.mask.mask_uuid or ""):
        remove_mask_resources(job.scene, old_composite, old_composite.mask)
    if _pointer(job.old_composite_image) and not job.reuse_old_composite_image:
        _remove_image(job.old_composite_image)
        detach_shared_owned_image(job.old_composite_image)
    _clear_recomposite_temporary_image_markers(job.old_composite_image)
    for index in sorted(
        (
            position
            for position, layer in enumerate(settings.layers)
            if str(layer.layer_uuid) in set(source_uuids)
        ),
        reverse=True,
    ):
        settings.layers.remove(index)
    if source_uuids:
        remove_owned_layer_bundles(
            settings.paint_material,
            source_uuids,
            source_images,
            detach_shared=True,
        )


def _commit_recomposite_creation(job: CompositeCreateJob) -> Any:
    """새 Bake 검증 뒤 기존 Composite를 평면 NORMAL Source로 바꾸고 과거 Source를 삭제합니다."""
    settings = job.settings
    if job.old_session is None or not _pointer(job.composite_image):
        raise CompositeLayerError("재합성 확정에 필요한 Composite Image가 없습니다.")
    if not _pointer(job.flatten_image):
        raise CompositeLayerError("재합성 확정에 필요한 평면 Source Image가 없습니다.")
    reference = job.old_session.composite.image
    reference_contract = _paint_image_contract(reference)
    if (
        _paint_image_contract(job.composite_image) != reference_contract
        or _paint_image_contract(job.flatten_image) != reference_contract
    ):
        raise CompositeLayerError("재합성 Image의 Paint Image 계약이 기존 Composite와 다릅니다.")
    _update_job_progress(job, 4, "Bake Validated", "평면 Source와 새 Composite Image를 검증했습니다")
    new_layer = settings.layers.add()
    new_layer.name = "Composite Layer"
    new_layer.layer_uuid = job.composite_uuid
    new_layer.layer_role = COMPOSITE_LAYER_ROLE
    new_layer.composite_session_uuid = job.session_uuid
    new_layer.composite_has_baked_alpha = bool(job.include_alpha)
    new_layer.image = job.composite_image
    new_layer.visible = True
    new_layer.expanded = False
    new_index = len(settings.layers) - 1
    if new_index != job.source_insert_index:
        settings.layers.move(new_index, job.source_insert_index)
    new_layer = find_layer(settings, job.composite_uuid)
    if new_layer is None:
        raise CompositeLayerError("새 Composite Layer UUID를 현재 목록에서 찾을 수 없습니다.")
    _set_recomposite_source_metadata(job, new_layer)
    new_layer.name = f"Composite Layer {new_layer.composite_generation}"
    settings.composite_state = "ACTIVE"
    settings.active_composite_session_uuid = job.session_uuid
    settings.composite_layer_uuid = job.composite_uuid
    settings.composite_source_schema_version = COMPOSITE_SOURCE_SCHEMA_VERSION
    settings.composite_saved_active_layer_uuid = str(job.source_layer_uuids[0])
    settings.active_layer_index = next(
        index
        for index, layer in enumerate(settings.layers)
        if str(layer.layer_uuid) == job.composite_uuid
    )
    settings.active_mask_uuid = ""
    settings.paint_mask_mode = False
    settings.overlay_image = job.composite_image
    settings.active_paint_image = job.composite_image
    reconcile_layer_graph(job.scene)
    validate_composite_session(job.scene)
    _update_job_progress(job, 5, "Shader Switched", "새 Composite Shader Chain을 검증했습니다")
    _remove_recomposited_composite_children(job)
    _clear_recomposite_temporary_image_markers(job.composite_image)
    _clear_recomposite_temporary_image_markers(job.flatten_image)
    reconcile_layer_graph(job.scene)
    validate_composite_session(job.scene)
    activate_layer_paint_image(SimpleNamespace(scene=job.scene), job.composite_uuid)
    _set_status(settings, "Composite Layer 다시 생성 완료", "SUCCESS")
    job.committed = True
    job.result_image = job.composite_image
    job.phase = "RESULT"
    _update_job_progress(job, 7, "Complete", "평면 Source 기반 Composite 작업 Layer를 사용할 수 있습니다")
    return job.result_image


def _commit_composite_creation(job: CompositeCreateJob) -> Any:
    """검증된 Bake Image를 Composite Layer로 확정하고 활성 Shader Chain으로 전환합니다."""
    if job.kind == "RECOMPOSITE":
        return _commit_recomposite_creation(job)
    settings = job.settings
    composite_image = job.composite_image
    if not _pointer(composite_image):
        raise CompositeLayerError("확정할 Composite Bake Image가 없습니다.")
    source_layers = _job_source_layers(job)
    if _paint_image_contract(composite_image) != _paint_image_contract(source_layers[0].image):
        raise CompositeLayerError("Composite Bake Image 계약이 원본 Layer와 다릅니다.")
    _update_job_progress(
        job,
        4,
        "Bake Validated",
        "베이크 결과 Paint Image 계약을 검증했습니다",
    )
    generation = max(
        (int(getattr(candidate, "composite_generation", 0)) for candidate in settings.layers),
        default=0,
    ) + 1
    layer = settings.layers.add()
    layer.name = f"Composite Layer {generation}"
    layer.layer_uuid = job.composite_uuid
    layer.layer_role = COMPOSITE_LAYER_ROLE
    layer.composite_session_uuid = job.session_uuid
    layer.composite_generation = generation
    layer.composite_has_baked_alpha = bool(job.include_alpha)
    layer.image = composite_image
    layer.visible = True
    layer.expanded = False
    new_index = len(settings.layers) - 1
    if new_index != job.source_insert_index:
        settings.layers.move(new_index, job.source_insert_index)
    settings.composite_layer_uuid = job.composite_uuid
    settings.composite_state = "ACTIVE"
    settings.composite_source_schema_version = COMPOSITE_SOURCE_SCHEMA_VERSION
    settings.active_layer_index = job.source_insert_index
    settings.active_mask_uuid = ""
    settings.paint_mask_mode = False
    settings.overlay_image = composite_image
    settings.active_paint_image = composite_image
    composite_layer = find_layer(settings, job.composite_uuid)
    if composite_layer is None:
        raise CompositeLayerError("생성한 Composite Layer UUID를 현재 목록에서 찾을 수 없습니다.")
    composite_layer.visible = True
    for source_layer in _job_source_layers(job):
        source_layer.visible = False
    reconcile_layer_graph(job.scene)
    validate_composite_session(job.scene)
    _update_job_progress(
        job,
        5,
        "Shader Switched",
        "원본 Layer Chain을 최종 출력에서 분리했습니다",
    )
    activate_layer_paint_image(
        SimpleNamespace(scene=job.scene),
        job.composite_uuid,
    )
    _update_job_progress(
        job,
        6,
        "Paint Target Ready",
        "Composite Image를 직접 Paint Target으로 확정했습니다",
    )
    _set_status(settings, "Composite Layer 생성 완료", "SUCCESS")
    job.committed = True
    job.result_image = composite_image
    job.phase = "RESULT"
    _update_job_progress(
        job,
        7,
        "Complete",
        "Composite 작업 Layer를 사용할 수 있습니다",
    )
    return composite_image


def _complete_composite_bake(context: Any, job: CompositeCreateJob) -> None:
    """Bake 완료 뒤 후처리 Modal에서 영구 복구·커밋과 UI 복구를 처리합니다."""
    try:
        if job.bake_preparation is not None:
            _restore_bake_persistent_state(job.bake_preparation)
        job.composite_image.update()
        _update_job_progress(
            job,
            3,
            "Bake Complete",
            "Cycles Base Color Bake가 완료되었습니다",
        )
        _commit_composite_creation(job)
    except Exception as exc:
        _rollback_composite_creation(job, exc, context=context)
        return
    preparation = job.ui_bake_preparation or job.bake_preparation
    job.bake_preparation = None
    job.ui_bake_preparation = None
    if preparation is None:
        return
    try:
        _restore_bake_ui_state(context, preparation)
    except Exception as ui_error:
        job.ui_restore_warning = f"UI 복구 경고: {ui_error}"
        _set_status(
            job.settings,
            "Composite Layer는 생성되었습니다. Texture Paint Mode 복구에 실패하여 "
            "Object Mode에 남아 있습니다. Painting Mode로 다시 진입하십시오.",
            "WARNING",
        )


def _fail_composite_bake(context: Any | None, job: CompositeCreateJob, exc: Exception) -> None:
    """Bake 전·후 실패를 롤백하고 UI 복구 실패는 경고로만 남깁니다."""
    _rollback_composite_creation(job, exc, context=context)


def release_composite_create_job(job: CompositeCreateJob) -> None:
    """결과 표시가 끝난 Composite 생성 Job과 후처리 진행 UI를 정리합니다."""
    _CREATE_JOBS.pop(job.session_uuid, None)
    release_composite_progress(job.progress)
    try:
        from .lifecycle import lifecycle_registry

        lifecycle_registry().schedule_pending_unregistration()
    except (ImportError, AttributeError, RuntimeError):
        pass


def clear_composite_create_jobs(*_args: Any) -> None:
    """수명주기 중 Bake 중 Job은 종료 뒤 롤백하도록 표시하고 나머지만 정리합니다."""
    for job in tuple(_CREATE_JOBS.values()):
        if job.phase == "NATIVE_BAKING":
            job.abort_after_bake = True
            continue
        try:
            if not job.committed and not job.rolled_back:
                _fail_composite_bake(
                    None,
                    job,
                    CompositeLayerError("Blender 상태 변경 전에 진행 중인 Composite Bake를 중단했습니다."),
                )
        finally:
            _CREATE_JOBS.pop(job.session_uuid, None)
            release_composite_progress(job.progress)


def composite_native_bake_pending() -> bool:
    """수명주기 경계가 즉시 정리하면 안 되는 Native Bake Job이 있는지 반환합니다."""
    return any(
        job.phase == "NATIVE_BAKING"
        for job in _CREATE_JOBS.values()
    )


def create_composite_layer(context: Any) -> Any:
    """Background와 직접 호출에서 Composite 생성 전 과정을 동기 Bake Transaction으로 실행합니다."""
    job = start_composite_layer_creation(context)
    try:
        prepare_composite_image(job)
        prepare_recomposite_flatten_image(context, job)
        if job.rolled_back:
            raise CompositeLayerError(job.error_message or "Composite Image 생성에 실패했습니다.")
        _bake_source_rgba(context, job.composite_image, job=job)
        _complete_composite_bake(context, job)
        if not job.committed:
            raise CompositeLayerError(job.error_message or "Composite 생성에 실패했습니다.")
        return job.result_image
    except Exception as exc:
        if not job.committed:
            _rollback_composite_creation(job, exc, context=context)
        raise
    finally:
        release_composite_create_job(job)


def _layer_bundle_images(layers: tuple[Any, ...]) -> tuple[Any, ...]:
    """Composite와 의존 형제 삭제 전에 소유 Image Pointer를 중복 없이 수집합니다."""
    images = []
    seen = set()
    for layer in layers:
        candidates = (layer.image, layer.mask.image) + tuple(
            adjustment.mask.image for adjustment in layer.adjustments
        )
        for image in candidates:
            pointer = _pointer(image)
            owned = bool(
                pointer
                and image.get(OWNER_KEY) == OWNER_TOKEN
                and image.get(RECORD_KIND_KEY) in {LAYER_IMAGE_ROLE, MASK_IMAGE_ROLE}
            )
            if owned and pointer not in seen:
                seen.add(pointer)
                images.append(image)
    return tuple(images)


def _restore_source_visibility(session: CompositeSession) -> None:
    """Composite 생성 전에 저장한 원본 Layer의 눈 아이콘 상태를 복구합니다."""
    for layer in session.sources:
        layer.visible = bool(layer.composite_saved_visible)


def _clear_source_metadata(settings: Any, source_uuids: tuple[str, ...]) -> None:
    """삭제 뒤 현재 Collection에서 Source UUID의 세션 Snapshot을 모두 제거합니다."""
    source_uuid_set = set(source_uuids)
    for layer in settings.layers:
        if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) != NORMAL_LAYER_ROLE:
            continue
        if str(layer.layer_uuid) not in source_uuid_set:
            continue
        layer.composite_source_session_uuid = ""
        layer.composite_bake_included = False
        layer.composite_saved_visible = False


def _capture_release_snapshot(
    scene: Any,
    settings: Any,
    session: CompositeSession,
) -> CompositeReleaseSnapshot:
    """원본 그래프 복구 전 변경될 Composite 활성 Scene·Node 상태를 캡처합니다."""
    del scene
    material = settings.paint_material
    tree = material.node_tree
    return CompositeReleaseSnapshot(
        composite_state=str(settings.composite_state),
        active_layer_index=int(settings.active_layer_index),
        overlay_image=settings.overlay_image,
        active_paint_image=settings.active_paint_image,
        paint_mask_mode=bool(settings.paint_mask_mode),
        active_mask_uuid=str(settings.active_mask_uuid or ""),
        source_visibility=tuple(
            (layer, bool(layer.visible)) for layer in session.sources
        ),
        active_node=tree.nodes.active,
        node_selection=tuple((node, bool(node.select)) for node in tree.nodes),
        paint_active_slot=int(material.paint_active_slot),
    )


def _restore_release_snapshot(
    scene: Any,
    settings: Any,
    snapshot: CompositeReleaseSnapshot,
) -> None:
    """복구 검증 실패 뒤 Composite 활성 상태의 가시성·선택·Node 상태를 되돌립니다."""
    settings.composite_state = snapshot.composite_state
    for layer, visible in snapshot.source_visibility:
        if _pointer(layer):
            layer.visible = visible
    settings.active_layer_index = snapshot.active_layer_index
    settings.overlay_image = snapshot.overlay_image
    settings.active_paint_image = snapshot.active_paint_image
    settings.paint_mask_mode = snapshot.paint_mask_mode
    settings.active_mask_uuid = snapshot.active_mask_uuid
    reconcile_layer_graph(scene)
    material = settings.paint_material
    for node, selected in snapshot.node_selection:
        if _pointer(node):
            node.select = selected
    if _pointer(snapshot.active_node):
        material.node_tree.nodes.active = snapshot.active_node
    material.paint_active_slot = snapshot.paint_active_slot
    settings.active_layer_index = snapshot.active_layer_index
    settings.overlay_image = snapshot.overlay_image
    settings.active_paint_image = snapshot.active_paint_image
    settings.paint_mask_mode = snapshot.paint_mask_mode
    settings.active_mask_uuid = snapshot.active_mask_uuid


def release_composite_layer(context: Any) -> None:
    """Composite만 논리 해제하고 독립 일반 Layer를 보존한 뒤 고아 자원을 정리합니다."""
    scene = context.scene
    settings = _settings(scene)
    if settings is None:
        raise CompositeLayerError("Projection Painting 설정이 없습니다.")
    from .overlay_migration import repair_managed_overlay_if_needed

    repair_managed_overlay_if_needed(context, overlay=settings.overlay_image)
    try:
        session = validate_composite_session(scene)
    except CompositeLayerError:
        settings.composite_state = "STALE"
        raise
    for source_layer in session.sources:
        if original_layer_is_read_only(settings, source_layer):
            continue
        repair_managed_overlay_if_needed(context, overlay=source_layer.image)
    deletion_layers = (session.composite,)
    deletion_uuids = tuple(str(layer.layer_uuid) for layer in deletion_layers)
    source_uuids = tuple(str(layer.layer_uuid) for layer in session.sources)
    deletion_images = _layer_bundle_images(deletion_layers)
    release_snapshot = _capture_release_snapshot(scene, settings, session)
    try:
        settings.composite_state = "RELEASING"
        _restore_source_visibility(session)
        saved_uuid = str(settings.composite_saved_active_layer_uuid or "")
        active_source = next(
            (
                layer
                for layer in settings.layers
                if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
                == NORMAL_LAYER_ROLE
                and str(layer.layer_uuid) == saved_uuid
            ),
            session.sources[0],
        )
        active_source_uuid = str(active_source.layer_uuid)
        settings.active_layer_index = next(
            index
            for index, layer in enumerate(settings.layers)
            if str(layer.layer_uuid) == active_source_uuid
        )
        settings.active_mask_uuid = ""
        settings.paint_mask_mode = False
        settings.overlay_image = active_source.image
        settings.active_paint_image = active_source.image
        reconcile_layer_graph(scene)
        validate_layer_graph(
            settings.paint_material,
            tuple(
                str(layer.layer_uuid)
                for layer in settings.layers
                if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE))
                == NORMAL_LAYER_ROLE
            ),
            expected_original_base=False,
        )
    except Exception as exc:
        try:
            _restore_release_snapshot(scene, settings, release_snapshot)
        except Exception as rollback_exc:
            settings.composite_state = "STALE"
            raise CompositeLayerError(
                f"원본 Layer Shader Chain 복구와 Composite 상태 롤백에 실패했습니다: {rollback_exc}"
            ) from exc
        raise CompositeLayerError(f"원본 Layer Shader Chain 복구에 실패했습니다: {exc}") from exc
    composite_index = next(
        index
        for index, layer in enumerate(settings.layers)
        if str(layer.layer_uuid) == deletion_uuids[0]
    )
    settings.layers.remove(composite_index)
    _clear_source_metadata(settings, source_uuids)
    _clear_composite_settings(settings)
    active_source_index = next(
        index
        for index, layer in enumerate(settings.layers)
        if str(layer.layer_uuid) == active_source_uuid
    )
    settings.active_layer_index = active_source_index
    settings.overlay_image = active_source.image
    settings.active_paint_image = (
        None if original_layer_is_read_only(settings, active_source) else active_source.image
    )
    settings.active_mask_uuid = ""
    settings.paint_mask_mode = False
    cleanup_error = None
    try:
        remove_owned_layer_bundles(
            settings.paint_material,
            deletion_uuids,
            deletion_images,
            detach_shared=True,
        )
    except Exception as exc:
        cleanup_error = exc
    if cleanup_error is None:
        _set_status(settings, "Composite Layer 해제 완료 · 독립 일반 Layer 보존", "SUCCESS")
        return
    _set_status(
        settings,
        f"Composite Layer 해제 완료 · 고아 자원 정리 경고: {cleanup_error}",
        "WARNING",
    )


def recover_interrupted_recomposite(scene: Any) -> None:
    """Load·Undo·Redo 뒤 남은 RECOMPOSITING 상태를 기존 ACTIVE 세션으로 안전하게 되돌립니다."""
    settings = _settings(scene)
    if settings is None or str(getattr(settings, "composite_state", "NONE")) != "RECOMPOSITING":
        return
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
    composites = tuple(
        layer
        for layer in settings.layers
        if str(getattr(layer, "layer_role", NORMAL_LAYER_ROLE)) == COMPOSITE_LAYER_ROLE
        and str(getattr(layer, "composite_session_uuid", "") or "") == session_uuid
    )
    if len(composites) != 1 or not _owned_layer_image(
        getattr(composites[0], "image", None),
        str(getattr(composites[0], "layer_uuid", "") or ""),
    ):
        settings.composite_state = "STALE"
        _set_status(settings, "중단된 Composite 다시 생성의 기존 세션을 검증할 수 없습니다.", "ERROR")
        return
    logical_images = _layer_bundle_images(tuple(settings.layers))
    logical_pointers = {_pointer(image) for image in logical_images}
    for image in tuple(bpy.data.images):
        if not _owned_layer_image(
            image,
            str(image.get(LAYER_UUID_KEY, "") or ""),
        ):
            continue
        image_session = str(image.get(COMPOSITE_IMAGE_SESSION_KEY, "") or "")
        transaction_uuid = str(image.get(RECOMPOSITE_TRANSACTION_KEY, "") or "")
        transaction_parent_uuid = str(
            image.get(RECOMPOSITE_PARENT_SESSION_KEY, "") or ""
        )
        if _pointer(image) in logical_pointers:
            if transaction_uuid or bool(image.get("pp_recomposite_pending_delete", False)):
                _clear_recomposite_temporary_image_markers(image)
            continue
        is_recomposite_temporary = bool(transaction_uuid) and transaction_parent_uuid == session_uuid
        is_legacy_recomposite_temporary = (
            image_session != session_uuid
            or str(image.get(LAYER_UUID_KEY, "") or "")
            == str(composites[0].layer_uuid)
        )
        if is_recomposite_temporary or is_legacy_recomposite_temporary:
            if int(getattr(image, "users", 0)) == 0:
                _remove_image(image)
            else:
                _clear_recomposite_temporary_image_markers(image)
    try:
        settings.composite_state = "ACTIVE"
        validate_composite_session(scene)
        _set_status(settings, "중단된 Composite 다시 생성을 기존 세션으로 복구했습니다.", "WARNING")
    except Exception as exc:
        settings.composite_state = "STALE"
        _set_status(settings, f"중단된 Composite 다시 생성 복구 실패: {exc}", "ERROR")


__all__ = [
    "COMPOSITE_IMAGE_SESSION_KEY",
    "CompositeLayerError",
    "CompositeCreateJob",
    "CompositeSession",
    "can_reuse_composite_image",
    "clear_composite_create_jobs",
    "composite_native_bake_pending",
    "composite_memory_estimate",
    "composite_memory_label",
    "composite_recompose_summary",
    "composite_release_summary",
    "combine_color_alpha_numpy",
    "create_final_texture_export_image",
    "create_texture_export_bake_images",
    "create_composite_layer",
    "finalize_native_bake",
    "invoke_native_bake",
    "invoke_prepared_emit_bake",
    "mark_native_bake_job_stopped",
    "mark_native_bake_outcome",
    "prepare_native_bake",
    "prepare_emit_bake",
    "prepare_composite_image",
    "prepare_recomposite_flatten_image",
    "prepare_recomposite_native_source",
    "recover_interrupted_recomposite",
    "remove_final_texture_export_image",
    "release_composite_create_job",
    "release_composite_layer",
    "rollback_native_bake",
    "restore_emit_bake_persistent_state",
    "restore_emit_bake_ui_state",
    "native_object_bake_running",
    "start_composite_native_progress",
    "start_composite_post_bake_progress",
    "start_composite_layer_creation",
    "validate_composite_session",
]
