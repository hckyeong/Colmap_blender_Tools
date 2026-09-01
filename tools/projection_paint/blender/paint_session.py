"""Blender Scene Pointer를 권위로 Texture Paint 진입과 종료를 자동화합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

import bpy
from bpy.props import BoolProperty, StringProperty

from .material_setup import (
    TARGET_KEY,
    PaintMaterialProgress,
    PaintMaterialSetup,
    PaintSetupError,
    create_paint_material_preparation_job,
    managed_overlay_recovery_required,
    pending_overlay_resolution_change,
    requires_udim_conversion,
    validate_paint_target,
)
from .paint_progress_session import (
    active_paint_progress_session,
    attach_paint_progress_ui,
    create_paint_progress_session,
    detach_paint_progress_ui,
    get_paint_progress_session,
    release_paint_progress_session,
)
from .paint_state import (
    PaintStateError,
    commit_paint_setup,
    derive_paint_state,
    validate_paint_contract,
)
from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    draw_progress_overlay,
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)
from .ui_messages import draw_wrapped_confirmation, invoke_wrapped_confirmation
class PaintSessionError(RuntimeError):
    """Texture Paint 자동화 수명주기 오류를 나타냅니다."""


@dataclass(frozen=True, slots=True)
class PaintSessionProgress:
    """Paint 이미지 준비부터 Paint 진입까지의 단계별 진행 상태를 보관합니다."""

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


class PaintSessionStartJob:
    """Texture Paint 진입 Generator를 Modal Timer 단위로 실행합니다."""

    def __init__(
        self,
        context: Any,
        target: Any,
        *,
        width: int,
        height: int,
        use_base_resolution: bool,
        replace_existing_overlay: bool,
    ) -> None:
        """현재 Context와 고정 옵션으로 지연 실행할 Paint 진입 작업을 만듭니다."""

        self._steps = _start_paint_session_steps(
            context,
            target,
            width=int(width),
            height=int(height),
            use_base_resolution=bool(use_base_resolution),
            replace_existing_overlay=bool(replace_existing_overlay),
        )
        self._replace_existing_overlay = bool(replace_existing_overlay)
        self.progress = PaintSessionProgress(
            (
                "Preparing resolution reset"
                if self._replace_existing_overlay
                else "Preparing paint image"
            ),
            0,
            16,
            f"대상: {getattr(target, 'name', 'Unknown')}",
        )
        self.result: PaintSession | None = None
        self.done = False

    def advance(self) -> PaintSessionProgress:
        """다음 Paint 준비 단계 하나를 실행하고 최신 진행값을 반환합니다."""

        if self.done:
            return self.progress
        try:
            self.progress = next(self._steps)
        except StopIteration as stop:
            self.result = stop.value
            self.done = True
            if self.result is None:
                raise PaintSessionError("Texture Paint 진입 결과가 없습니다.")
            self.progress = PaintSessionProgress(
                "Texture Paint ready",
                16,
                16,
                f"대상: {getattr(self.result.setup.target, 'name', 'Unknown')}",
            )
        return self.progress

    def abort(self) -> None:
        """완료 전 Paint 진입 Generator를 닫아 준비 상태를 복구합니다."""

        if self.done:
            return
        self._steps.close()
        self.done = True

    @property
    def conversion_ready(self) -> bool:
        """Modal 밖에서 Texture Paint 전환을 이어갈 안전한 경계인지 반환합니다."""

        boundary = (
            "Resolution reset complete"
            if self._replace_existing_overlay
            else "Paint image ready"
        )
        return self.progress.phase == boundary

    @property
    def progress_total_units(self) -> int:
        """현재 Modal에서 표시할 실제 준비 단계의 마지막 단위를 반환합니다."""

        return 14 if self._replace_existing_overlay else 11

    @property
    def progress_title(self) -> str:
        """최초 변환과 해상도 초기화에 맞는 중앙 진행창 제목을 반환합니다."""

        return (
            "Paint Resolution Reset"
            if self._replace_existing_overlay
            else "Texture Paint Preparation"
        )


_OVERLAY_RESOLUTIONS = {
    "RES_1K": 1024,
    "RES_2K": 2048,
    "RES_4K": 4096,
    "RES_8K": 8192,
}
_PAINT_PROGRESS_TIMER_INTERVAL_SECONDS = 0.025


def _paint_progress_ui_available(context: Any) -> bool:
    """현재 실행 환경에서 Paint 중앙 진행창을 연결할 수 있는지 확인합니다."""

    return not bpy.app.background and context.window is not None


def _selected_overlay_resolution(settings: Any) -> tuple[int, int]:
    """Scene의 1K·2K·4K·8K 선택값을 정사각 Pixel 크기로 변환합니다."""
    value = _OVERLAY_RESOLUTIONS.get(str(settings.overlay_resolution))
    if value is None:
        raise PaintSessionError("지원하지 않는 Paint Overlay 해상도입니다.")
    return value, value


def _resolution_label(size: tuple[int, int]) -> str:
    """Pixel 크기를 경고창에 표시할 K 또는 가로×세로 문자열로 변환합니다."""
    width, height = size
    labels = {1024: "1K", 2048: "2K", 4096: "4K", 8192: "8K"}
    if width == height and width in labels:
        return labels[width]
    return f"{width} × {height}"


def _resolution_reset_message(
    current: tuple[int, int],
    desired: tuple[int, int],
) -> str:
    """해상도 변경 시 전체 Layer 초기화를 알리는 경고 문구를 반환합니다."""
    return (
        f"현재 {_resolution_label(current)} Paint 이미지와 모든 Layer·Mask·보정 작업을 "
        f"삭제하고 새 {_resolution_label(desired)} Layer 1로 초기화합니다. "
        "되돌리기는 Blender Undo 기록이 있을 때만 가능합니다."
    )


def _pointer(value: Any) -> int:
    """삭제된 Blender ID를 안전하게 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _paint_entry_overlay_for_target(settings: Any, target: Any):
    """현재 Target이 소유한 Scene Overlay만 진입 전 복구 대상으로 반환합니다."""

    overlay = getattr(settings, "overlay_image", None)
    if not _pointer(target) or not _pointer(overlay):
        return None
    target_uuid = str(target.get(TARGET_KEY, "") or "")
    overlay_uuid = str(overlay.get(TARGET_KEY, "") or "")
    if not target_uuid or overlay_uuid != target_uuid:
        return None
    return overlay


_TARGET_SCENE_STATE_DEFAULTS = {
    "paint_material": None,
    "overlay_image": None,
    "active_paint_image": None,
    "paint_mask_mode": False,
    "paint_mesh_uuid": "",
    "paint_uv_name": "",
    "paint_material_slot_index": 0,
    "paint_material_slot_link": "",
    "active_layer_index": -1,
    "layer_selection_anchor_uuid": "",
    "active_mask_uuid": "",
    "layer_preview_uuid": "",
    "mask_preview_adjustment_uuid": "",
    "composite_state": "NONE",
    "active_composite_session_uuid": "",
    "composite_source_schema_version": 0,
    "composite_layer_uuid": "",
    "composite_saved_active_layer_uuid": "",
    "original_layer_name": "Original Layer",
    "original_layer_visible": True,
    "original_layer_preview": False,
    "original_layer_schema_version": 0,
    "original_source_image": None,
    "original_source_color": (0.8, 0.8, 0.8, 1.0),
    "original_source_node_uuid": "",
    "original_source_socket_identifier": "",
    "durability_state": "EMPTY",
}


def _paint_entry_has_foreign_state(settings: Any, target: Any) -> bool:
    """현재 Target과 다른 애드온 소유 Paint 자원이 남아 있는지 반환합니다."""

    from .layer_shader import original_layer_is_read_only

    candidates = [
        getattr(settings, "paint_material", None),
        getattr(settings, "overlay_image", None),
        getattr(settings, "active_paint_image", None),
    ]
    for layer in getattr(settings, "layers", ()):
        if not original_layer_is_read_only(settings, layer):
            candidates.append(layer.image)
        candidates.append(layer.mask.image)
        for adjustment in layer.adjustments:
            candidates.extend((adjustment.image, adjustment.mask.image))
    owned_candidates = tuple(item for item in candidates if _pointer(item))
    if not owned_candidates:
        return False
    target_uuid = str(target.get(TARGET_KEY, "") or "") if _pointer(target) else ""
    if not target_uuid:
        return True
    return any(
        str(item.get(TARGET_KEY, "") or "") != target_uuid
        for item in owned_candidates
    )


def _snapshot_target_scene_state(settings: Any) -> dict[str, Any]:
    """Target 교체 실패 시 복구할 Scene Layer와 관련 속성을 값으로 보관합니다."""

    from .layer_reset import _snapshot_layer

    values = {
        name: getattr(settings, name)
        for name in _TARGET_SCENE_STATE_DEFAULTS
    }
    values["original_source_color"] = tuple(settings.original_source_color)
    return {
        "values": values,
        "layers": tuple(_snapshot_layer(layer) for layer in settings.layers),
    }


def _clear_target_scene_state(settings: Any) -> None:
    """신규 Target을 준비하기 전에 이전 Target의 활성 Scene 참조만 비웁니다."""

    settings.layers.clear()
    for name, value in _TARGET_SCENE_STATE_DEFAULTS.items():
        setattr(settings, name, value)


def _restore_target_scene_state(settings: Any, snapshot: dict[str, Any]) -> None:
    """신규 Target 진입 실패 뒤 이전 Target의 Scene 참조와 Layer를 복원합니다."""

    from .layer_reset import _replace_layer_collection

    for name, value in snapshot["values"].items():
        setattr(settings, name, value)
    _replace_layer_collection(settings, snapshot["layers"])


def _settings(scene: Any):
    """등록된 Projection Paint Scene 설정을 반환합니다."""
    return getattr(scene, "projection_paint", None)


def _set_status(scene: Any, message: str, kind: str, paint_state: str) -> None:
    """Scene UI 상태 Cache를 실제 작업 결과에 맞게 갱신합니다."""
    settings = _settings(scene)
    if settings is None:
        return
    settings.status = str(message)
    settings.status_kind = str(kind)
    settings.paint_state = str(paint_state)


def _finite_background_color(value: Any) -> tuple[float, float, float] | None:
    """뷰포트 배경색을 직렬화 가능한 유한한 RGB 값으로 검증합니다."""

    try:
        components = tuple(value)
    except (TypeError, ValueError):
        return None
    if len(components) != 3:
        return None
    color: list[float] = []
    for component in components:
        if isinstance(component, bool):
            return None
        try:
            number = float(component)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        color.append(number)
    return color[0], color[1], color[2]


def _capture_viewport_background(context: Any) -> dict[str, Any] | None:
    """호출한 3D Viewport의 유효한 배경 표시값만 캡처합니다."""

    space = getattr(context, "space_data", None)
    shading = getattr(space, "shading", None)
    if getattr(space, "type", "") != "VIEW_3D" or shading is None:
        return None
    background_type = getattr(shading, "background_type", None)
    background_color = _finite_background_color(
        getattr(shading, "background_color", None)
    )
    if not isinstance(background_type, str) or not background_type or background_color is None:
        return None
    return {
        "background_type": background_type,
        "background_color": list(background_color),
    }


def _apply_captured_viewport_background(space: Any, payload: dict[str, Any]) -> None:
    """검증된 호출 뷰포트 배경을 전용 Paint Viewport에 안전하게 적용합니다."""

    shading = getattr(space, "shading", None)
    background = payload.get("source_viewport_background")
    if shading is None or not isinstance(background, dict):
        return
    background_type = background.get("background_type")
    background_color = _finite_background_color(background.get("background_color"))
    if not isinstance(background_type, str) or not background_type or background_color is None:
        return
    previous_type = str(getattr(shading, "background_type", "") or "")
    previous_color = _finite_background_color(
        getattr(shading, "background_color", None)
    )
    try:
        shading.background_type = background_type
        shading.background_color = background_color
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        try:
            if previous_type:
                shading.background_type = previous_type
            if previous_color is not None:
                shading.background_color = previous_color
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass


@dataclass(frozen=True, slots=True)
class PaintSession:
    """Scene Pointer에서 매번 복원하는 비권위 Paint 계약 Snapshot입니다."""

    scene: Any
    setup: PaintMaterialSetup
    state: str

    def validate(self, _token: Any = None) -> None:
        """현재 Scene의 Blender Material 계약을 다시 검증합니다."""
        validate_paint_contract(self.scene)


def active_paint_session(scene: Any | None = None) -> PaintSession | None:
    """실제 Target Texture Paint와 Material 계약이 유효하면 현재 Snapshot을 반환합니다."""
    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    settings = _settings(active_scene) if active_scene is not None else None
    target = getattr(settings, "paint_target", None) if settings is not None else None
    if settings is None or not _pointer(target) or target.mode != "TEXTURE_PAINT":
        return None
    try:
        setup = validate_paint_contract(active_scene)
    except PaintStateError:
        return None
    state, _reason = derive_paint_state(active_scene)
    return PaintSession(active_scene, setup, state)


def paint_runtime_is_active(scene: Any) -> bool:
    """Paint 상태가 ACTIVE이고 실제 Scene Runtime이 설치된 경우에만 참을 반환합니다."""

    settings = getattr(scene, "projection_paint", None)
    if settings is None or str(settings.paint_state) != "ACTIVE":
        return False
    from .paint_runtime import active_paint_runtime

    return active_paint_runtime(scene) is not None


def validate_active_paint_session(_token: Any = None) -> PaintSession:
    """현재 Blender Pointer에서 활성 Paint Snapshot을 검증해 반환합니다."""
    session = active_paint_session()
    if session is None:
        raise PaintSessionError("활성 Texture Paint 자동화 상태가 없습니다.")
    session.validate()
    return session


def _capture_snapshot(context: Any, target: Any) -> None:
    """Paint 진입 전 Workspace·선택·Mode·가시성 상태를 Scene에 직렬화합니다."""
    scene = context.scene
    settings = scene.projection_paint
    window = getattr(context, "window", None)
    space = getattr(context, "space_data", None)
    source_viewport_background = _capture_viewport_background(context)
    current_workspace = getattr(window, "workspace", None)
    restore_workspace_name = str(getattr(current_workspace, "name", "") or "")
    try:
        from .paint_workspace import ORIGIN_WORKSPACE_KEY, WORKSPACE_KIND
        from .ownership import is_owned

        if is_owned(current_workspace, kind=WORKSPACE_KIND):
            restore_workspace_name = str(
                current_workspace.get(
                    ORIGIN_WORKSPACE_KEY,
                    restore_workspace_name,
                )
                or restore_workspace_name
            )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    snapshot = {
        "workspace": restore_workspace_name,
        "mode": str(context.mode),
        "selected": [str(obj.name) for obj in tuple(context.selected_objects)],
        "target_hide_viewport": bool(target.hide_viewport),
        "target_hide_view_layer": bool(target.hide_get(view_layer=context.view_layer)),
        "review_background_enabled": bool(settings.review_background_enabled),
        "review_background_changed": bool(settings.review_background_enabled),
        "shading": (
            str(space.shading.type)
            if getattr(space, "type", "") == "VIEW_3D"
            else ""
        ),
    }
    if source_viewport_background is not None:
        snapshot["source_viewport_background"] = source_viewport_background
    from .point_scene import hide_dataset_points, restore_dataset_points

    point_visibility = hide_dataset_points(scene, context.view_layer)
    snapshot["point_visibility"] = point_visibility
    try:
        settings.paint_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        settings.original_active_object = context.view_layer.objects.active
        if snapshot["review_background_changed"]:
            settings.review_background_enabled = False
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        restore_dataset_points(scene, context.view_layer, point_visibility)
        if snapshot["review_background_changed"]:
            settings.review_background_enabled = bool(
                snapshot["review_background_enabled"]
            )
        settings.paint_snapshot_json = "{}"
        raise


def _snapshot_payload(scene: Any) -> dict[str, Any]:
    """Scene에 직렬화된 Paint 진입 전 상태를 안전하게 읽습니다."""
    try:
        payload = json.loads(str(scene.projection_paint.paint_snapshot_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _store_snapshot_payload(scene: Any, payload: dict[str, Any]) -> None:
    """갱신된 Paint 복구 Snapshot을 Scene에 UTF-8 JSON으로 저장합니다."""
    scene.projection_paint.paint_snapshot_json = json.dumps(
        payload,
        ensure_ascii=False,
    )


def _object_in_view_layer(view_layer: Any, obj: Any) -> bool:
    """Object가 지정한 View Layer에 아직 존재하는지 확인합니다."""
    if obj is None or _pointer(obj) == 0 or _pointer(view_layer) == 0:
        return False
    try:
        return view_layer.objects.get(obj.name) is obj
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _set_target_active(context: Any, target: Any) -> None:
    """현재 View Layer에서 Paint Target만 활성 선택합니다."""
    for obj in tuple(context.selected_objects):
        obj.select_set(False, view_layer=context.view_layer)
    target.select_set(True, view_layer=context.view_layer)
    context.view_layer.objects.active = target


def _enter_texture_paint(context: Any, target: Any) -> None:
    """전용 작업공간에서 활성 Target의 Texture Paint 진입을 정확히 한 번 시도합니다."""
    _set_target_active(context, target)
    window = getattr(context, "window", None)
    area, region, _space = _window_view_parts(window)
    if window is None or area is None or region is None:
        raise PaintSessionError("Texture Paint Mode 진입에 사용할 3D Viewport가 없습니다.")
    with context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    if result != {"FINISHED"} or target.mode != "TEXTURE_PAINT":
        raise PaintSessionError("Texture Paint Mode 진입에 실패했습니다.")


def _activate_current_paint_image(context: Any) -> Any:
    """현재 Scene이 확정한 Layer 또는 Mask Paint Image를 활성화합니다."""
    settings = context.scene.projection_paint
    active_index = int(settings.active_layer_index)
    if active_index < 0 or active_index >= len(settings.layers):
        raise PaintSessionError("활성 Paint Layer Index가 유효하지 않습니다.")
    active_layer = settings.layers[active_index]
    from .layer_shader import activate_layer_paint_image, activate_mask_paint_image

    if bool(settings.paint_mask_mode) and str(settings.active_mask_uuid or ""):
        return activate_mask_paint_image(
            context,
            str(active_layer.layer_uuid),
            str(settings.active_mask_uuid),
        )
    return activate_layer_paint_image(context, str(active_layer.layer_uuid))


def _switch_paint_workspace(context: Any) -> None:
    """현재 Window의 준비된 Paint 작업공간을 재사용하거나 새로 생성합니다."""
    from .paint_workspace import ensure_paint_workspace

    ensure_paint_workspace(context, reuse_existing=True)


def _window_view_parts(window: Any):
    """지정한 Window의 첫 3D Viewport 구성요소를 반환합니다."""
    if window is None or _pointer(window) == 0:
        return None, None, None
    area = next(
        (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
        None,
    )
    region = next(
        (candidate for candidate in getattr(area, "regions", ()) if candidate.type == "WINDOW"),
        None,
    )
    space = getattr(getattr(area, "spaces", None), "active", None)
    if area is None or region is None or space is None or space.type != "VIEW_3D":
        return None, None, None
    return area, region, space


def _prepare_paint_view(context: Any, session: PaintSession) -> tuple[Any, Any]:
    """전환된 작업공간에서 Target 가시성과 Diffuse Color 표시를 준비합니다."""
    target = session.setup.target
    window = getattr(context, "window", None)
    if window is None or _pointer(window) == 0:
        raise PaintSessionError("Texture Paint를 표시할 Blender Window가 없습니다.")
    if not _object_in_view_layer(context.view_layer, target):
        raise PaintSessionError("Paint Target이 현재 View Layer에 없습니다.")
    area, region, space = _window_view_parts(window)
    if area is None or region is None or space is None:
        raise PaintSessionError("Texture Paint Workspace에 3D Viewport가 없습니다.")
    payload = _snapshot_payload(session.scene)
    with context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        if target.hide_viewport:
            payload["target_hide_viewport_changed"] = True
            target.hide_viewport = False
        if target.hide_get(view_layer=context.view_layer):
            payload["target_hide_view_layer_changed"] = True
            target.hide_set(False, view_layer=context.view_layer)
        _set_target_active(bpy.context, target)
        payload["paint_shading"] = str(space.shading.type)
        payload["paint_render_pass"] = str(space.shading.render_pass)
        payload["paint_shading_changed"] = bool(
            space.shading.type != "MATERIAL"
            or space.shading.render_pass != "DIFFUSE_COLOR"
        )
        space.shading.type = "MATERIAL"
        space.shading.render_pass = "DIFFUSE_COLOR"
        _apply_captured_viewport_background(space, payload)
        if not target.visible_get(view_layer=context.view_layer, viewport=space):
            try:
                payload["paint_local_view"] = bool(target.local_view_get(space))
                target.local_view_set(space, True)
                payload["paint_local_view_changed"] = bool(
                    not payload["paint_local_view"]
                    and target.local_view_get(space)
                )
            except RuntimeError as exc:
                raise PaintSessionError(
                    "현재 Texture Paint Viewport에서 Paint Target을 표시할 수 없습니다."
                ) from exc
    _store_snapshot_payload(session.scene, payload)
    return area, region


def _restore_snapshot(context: Any, overlay: Any) -> None:
    """Scene에 보존한 값에 한해 진입 전 사용자 상태를 복구합니다."""
    scene = context.scene
    settings = scene.projection_paint
    payload = _snapshot_payload(scene)
    window = getattr(context, "window", None)
    target = settings.paint_target
    _area, _region, paint_space = _window_view_parts(window)
    if paint_space is not None and _pointer(target):
        try:
            if (
                bool(payload.get("paint_shading_changed", False))
                and paint_space.shading.type == "MATERIAL"
                and paint_space.shading.render_pass == "DIFFUSE_COLOR"
            ):
                paint_space.shading.type = str(payload.get("paint_shading", "SOLID"))
                paint_space.shading.render_pass = str(
                    payload.get("paint_render_pass", "COMBINED")
                )
            if (
                bool(payload.get("paint_local_view_changed", False))
                and target.local_view_get(paint_space)
            ):
                target.local_view_set(
                    paint_space,
                    bool(payload.get("paint_local_view", False)),
                )
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    workspace = bpy.data.workspaces.get(str(payload.get("workspace", "")))
    if window is not None and workspace is not None:
        window.workspace = workspace
    try:
        from .point_scene import restore_dataset_points

        restore_dataset_points(
            scene,
            context.view_layer,
            payload.get("point_visibility", ()),
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    try:
        if (
            _pointer(target)
            and bool(payload.get("target_hide_viewport_changed", False))
            and not target.hide_viewport
        ):
            target.hide_viewport = bool(payload.get("target_hide_viewport", False))
        if (
            _pointer(target)
            and bool(payload.get("target_hide_view_layer_changed", False))
            and not target.hide_get(view_layer=context.view_layer)
        ):
            target.hide_set(
                bool(payload.get("target_hide_view_layer", False)),
                view_layer=context.view_layer,
            )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass
    try:
        for obj in tuple(context.selected_objects):
            obj.select_set(False, view_layer=context.view_layer)
        for name in payload.get("selected", ()):
            obj = context.view_layer.objects.get(str(name))
            if obj is not None:
                obj.select_set(True, view_layer=context.view_layer)
        original_active = settings.original_active_object
        context.view_layer.objects.active = (
            original_active
            if _object_in_view_layer(context.view_layer, original_active)
            else None
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass
    try:
        if (
            bool(payload.get("review_background_changed", False))
            and not bool(settings.review_background_enabled)
        ):
            settings.review_background_enabled = bool(
                payload.get("review_background_enabled", True)
            )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    settings.paint_snapshot_json = "{}"
    settings.original_active_object = None


def _restore_snapshot_and_keep_workspace(context: Any, overlay: Any) -> None:
    """사용자 상태를 복구하고 READY Paint Workspace는 다음 진입에 재사용합니다."""

    _restore_snapshot(context, overlay)

    # Align·Modifier와 동일하게 정상 종료한 READY Workspace는 삭제하지 않습니다.
    # from .paint_workspace import remove_paint_workspace
    # remove_paint_workspace(
    #     context.scene,
    #     window=getattr(context, "window", None),
    #     workspace=getattr(context.scene.projection_paint, "paint_workspace", None),
    # )


def _session_material_progress(
    progress: PaintMaterialProgress,
) -> PaintSessionProgress:
    """Material 준비 11단계를 Paint 진입 전체 16단계에 그대로 매핑합니다."""

    return PaintSessionProgress(
        progress.phase,
        min(int(progress.completed_units), 11),
        16,
        str(progress.detail),
    )


def _start_paint_session_steps(
    context: Any,
    target: Any,
    *,
    width: int = 2048,
    height: int = 2048,
    use_base_resolution: bool = True,
    replace_existing_overlay: bool = False,
) -> Any:
    """Scene Pointer에 Paint 상태를 원자적으로 준비하며 실제 단계마다 진행값을 내보냅니다."""
    active_elsewhere = any(
        scene is not context.scene
        and _pointer(getattr(getattr(scene, "projection_paint", None), "paint_target", None))
        and getattr(scene.projection_paint.paint_target, "mode", "") == "TEXTURE_PAINT"
        for scene in bpy.data.scenes
    )
    if active_elsewhere or bool(context.scene.projection_paint.paint_enabled):
        raise PaintSessionError("다른 Window 또는 Scene에서 Paint 자동화가 이미 활성화되어 있습니다.")
    yield PaintSessionProgress(
        "Preparing paint state",
        0,
        16,
        f"대상: {target.name}",
    )
    settings = context.scene.projection_paint
    composite_state = str(getattr(settings, "composite_state", "NONE"))
    foreign_state = _paint_entry_has_foreign_state(settings, target)
    if foreign_state and composite_state != "NONE":
        raise PaintSessionError(
            "이전 Paint Target의 Composite를 먼저 해제한 뒤 새 Target을 선택하십시오."
        )
    if composite_state not in {"NONE", "ACTIVE"}:
        raise PaintSessionError(
            "Composite 처리 중이거나 손상된 상태에서는 Painting Mode에 진입할 수 없습니다."
        )
    if replace_existing_overlay and composite_state != "NONE":
        raise PaintSessionError("Composite를 먼저 해제한 뒤 해상도를 변경하십시오.")
    from .overlay_migration import repair_managed_overlay_if_needed

    recovery_overlay = _paint_entry_overlay_for_target(settings, target)
    if recovery_overlay is not None:
        repair_managed_overlay_if_needed(context, overlay=recovery_overlay)
    previous_authority = (
        settings.paint_target,
        settings.paint_material,
        settings.overlay_image,
        settings.active_paint_image,
        bool(settings.paint_mask_mode),
    )
    previous_target_scene_state = (
        _snapshot_target_scene_state(settings) if foreign_state else None
    )
    reset_existing_layers = bool(
        replace_existing_overlay
        and len(settings.layers) > 0
        and settings.paint_target is target
        and settings.paint_material is target.active_material
    )
    _capture_snapshot(context, target)
    setup = None
    material_job = None
    _set_status(context.scene, "Texture Paint 준비 중", "RUNNING", "ENTERING")
    try:
        material_job = create_paint_material_preparation_job(
            target,
            width,
            height,
            use_base_resolution=use_base_resolution,
            replace_existing_overlay=(
                replace_existing_overlay and not reset_existing_layers
            ),
        )
        while not material_job.done:
            yield _session_material_progress(material_job.advance())
        setup = material_job.result
        if setup is None:
            raise PaintSessionError("Paint Material 준비 결과가 없습니다.")
        yield PaintSessionProgress(
            "Paint image ready",
            11,
            16,
            f"대상: {target.name}",
        )
        yield PaintSessionProgress(
            "Creating Original Layer",
            13,
            16,
            f"대상: {target.name} · 현재 Stack 해상도로 Original 작업 복사본 생성",
        )
        if previous_target_scene_state is not None:
            _clear_target_scene_state(settings)
        commit_paint_setup(context.scene, setup)
        from .layer_shader import (
            reconcile_layer_graph,
        )

        reconcile_layer_graph(
            context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
        if reset_existing_layers:
            from .layer_reset import replace_with_fresh_layer

            yield PaintSessionProgress(
                "Preparing Original source",
                12,
                16,
                f"대상: {target.name} · 보존된 원본과 UDIM 타일 확인",
            )
            replace_with_fresh_layer(
                context,
                int(width),
                int(height),
                use_base_resolution=use_base_resolution,
            )
            setup = validate_paint_contract(context.scene)
            yield PaintSessionProgress(
                "Resolution reset complete",
                14,
                16,
                f"대상: {target.name} · Original 리샘플과 Layer Stack 교체 완료",
            )
        elif replace_existing_overlay:
            yield PaintSessionProgress(
                "Resolution reset complete",
                14,
                16,
                f"대상: {target.name} · 새 Original과 Layer Stack 준비 완료",
            )
        else:
            yield PaintSessionProgress(
                "Activating paint layer",
                14,
                16,
                f"대상: {target.name}",
            )
        _activate_current_paint_image(context)
        _set_target_active(context, target)
        yield PaintSessionProgress(
            "Switching paint workspace",
            15,
            16,
            f"대상: {target.name}",
        )
        _switch_paint_workspace(context)
        active_image = settings.active_paint_image or setup.overlay
        settings.durability_state = (
            "DIRTY" if bool(active_image.is_dirty) else "EMPTY"
        )
        setup.commit()
        _set_status(context.scene, "Texture Paint 작업공간 전환 중", "RUNNING", "ENTERING")
        yield PaintSessionProgress(
            "Texture Paint ready",
            16,
            16,
            f"대상: {target.name}",
        )
        return PaintSession(context.scene, setup, "ACTIVE")
    except (Exception, GeneratorExit):
        settings.paint_enabled = False
        if setup is not None:
            setup.rollback()
        elif material_job is not None:
            material_job.abort()
        (
            settings.paint_target,
            settings.paint_material,
            settings.overlay_image,
            settings.active_paint_image,
            settings.paint_mask_mode,
        ) = previous_authority
        if previous_target_scene_state is not None:
            _restore_target_scene_state(settings, previous_target_scene_state)
        _restore_snapshot_and_keep_workspace(
            context,
            setup.overlay if setup is not None else None,
        )
        _set_status(context.scene, "Texture Paint 준비 실패", "ERROR", "ERROR")
        raise


def create_paint_session_start_job(
    context: Any,
    target: Any,
    *,
    width: int = 2048,
    height: int = 2048,
    use_base_resolution: bool = True,
    replace_existing_overlay: bool = False,
) -> PaintSessionStartJob:
    """동기 실행과 Modal UI가 공유할 Texture Paint 진입 작업을 생성합니다."""

    return PaintSessionStartJob(
        context,
        target,
        width=width,
        height=height,
        use_base_resolution=use_base_resolution,
        replace_existing_overlay=replace_existing_overlay,
    )


def start_paint_session(
    context: Any,
    target: Any,
    *,
    width: int = 2048,
    height: int = 2048,
    use_base_resolution: bool = True,
    replace_existing_overlay: bool = False,
) -> PaintSession:
    """단계별 진입 작업을 끝까지 실행해 기존 동기 호출 계약을 유지합니다."""

    job = create_paint_session_start_job(
        context,
        target,
        width=width,
        height=height,
        use_base_resolution=use_base_resolution,
        replace_existing_overlay=replace_existing_overlay,
    )
    while not job.done:
        job.advance()
    if job.result is None:
        raise PaintSessionError("Texture Paint 진입 결과가 없습니다.")
    return job.result


def _complete_paint_entry(context: Any, scene_pointer: int) -> None:
    """전환된 3D Viewport에서 Paint·Source 자원만 준비하고 Brush는 변경하지 않습니다."""
    session = active_paint_session()
    if session is None or _pointer(session.scene) != scene_pointer:
        raise PaintSessionError("Paint 진입 상태가 Workspace 전환 중 변경되었습니다.")
    area, region = _prepare_paint_view(context, session)
    window = context.window
    settings = session.scene.projection_paint
    from .paint_runtime import start_paint_runtime
    from .stencil_session import start_stencil_session

    with context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        _activate_current_paint_image(bpy.context)
        start_stencil_session(
            bpy.context,
            dataset_uuid=str(settings.dataset_uuid),
            source_fingerprint=str(settings.source_fingerprint),
        )
        start_paint_runtime(bpy.context, session, reconnect_on_start=False)
    from .paint_runtime import active_paint_runtime

    if active_paint_runtime(session.scene) is None:
        raise PaintSessionError("Texture Paint Runtime 설치를 확인하지 못했습니다.")
    settings.paint_enabled = True
    _set_status(session.scene, "Texture Paint 준비 완료", "SUCCESS", "ACTIVE")


def _rollback_failed_paint_entry(context: Any, scene: Any) -> str:
    """진입 단계와 무관하게 Paint·Stencil Runtime과 사용자 상태를 정식 복구합니다."""

    settings = getattr(scene, "projection_paint", None)
    target = getattr(settings, "paint_target", None) if settings is not None else None
    snapshot_payload = str(
        getattr(settings, "paint_snapshot_json", "") or ""
    ) if settings is not None else ""
    should_restore = bool(
        settings is not None
        and (
            str(settings.paint_state) == "ENTERING"
            or snapshot_payload not in {"", "{}"}
            or str(getattr(target, "mode", "")) == "TEXTURE_PAINT"
        )
    )
    if not should_restore:
        if settings is not None:
            settings.paint_enabled = False
        return ""
    try:
        finish_paint_session(context)
    except (
        AttributeError,
        PaintSessionError,
        ReferenceError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as cleanup_error:
        if settings is not None:
            settings.paint_enabled = False
        return str(cleanup_error)
    return ""


def _schedule_paint_entry(context: Any, session: PaintSession) -> None:
    """Workspace 화면 전환 뒤 초기 Paint View 연결을 한 번 예약합니다."""
    scene_pointer = _pointer(session.scene)
    progress = {"attempts": 0, "configured": False, "mode_attempted": False}

    def complete_after_workspace() -> float | None:
        """Texture Paint Workspace가 준비되면 초기 Stencil 연결을 완료합니다."""
        current = session
        if _pointer(current.scene) != scene_pointer:
            return None
        try:
            current.validate()
            validation_error = None
        except PaintStateError as exc:
            validation_error = PaintSessionError(str(exc))
        expected = getattr(current.scene.projection_paint, "paint_workspace", None)
        window = getattr(bpy.context, "window", None)
        if expected is None or window is None or window.workspace is not expected:
            progress["attempts"] += 1
            if progress["attempts"] < 200:
                return 0.025
            error = PaintSessionError("전용 Paint 작업공간 전환 시간이 초과되었습니다.")
        else:
            error = validation_error
        try:
            if error is not None:
                raise error
            if not bool(progress["configured"]):
                from .paint_workspace import configure_paint_workspace

                if not configure_paint_workspace(bpy.context):
                    return 0.025
                progress["configured"] = True
                return 0.025
            target = current.setup.target
            if target.mode != "TEXTURE_PAINT":
                if bool(progress["mode_attempted"]):
                    raise PaintSessionError("전용 작업공간의 Texture Paint 전환에 실패했습니다.")
                progress["mode_attempted"] = True
                _enter_texture_paint(bpy.context, target)
                return 0.025
            _complete_paint_entry(bpy.context, scene_pointer)
        except (
            AttributeError,
            PaintSessionError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            scene = current.scene
            cleanup_error = _rollback_failed_paint_entry(bpy.context, scene)
            detail = f" | 정리 실패: {cleanup_error}" if cleanup_error else ""
            _set_status(
                scene,
                f"Texture Paint 준비 실패: {exc}{detail}",
                "ERROR",
                "ERROR",
            )
        return None

    from .lifecycle import lifecycle_registry

    lifecycle_registry().register_timer(
        complete_after_workspace,
        component="paint_entry",
        first_interval=0.0,
    )


def finish_paint_session(context: Any) -> None:
    """Texture Paint를 종료하고 Overlay를 보존한 채 사용자 상태를 복구합니다."""
    scene = context.scene
    settings = _settings(scene)
    if settings is None:
        return
    overlay = settings.active_paint_image or settings.overlay_image
    target = settings.paint_target
    try:
        from .paint_runtime import stop_paint_runtime

        stop_paint_runtime(context, scene=scene)
    except (ImportError, AttributeError, RuntimeError):
        pass
    try:
        from .stencil_session import finish_stencil_session

        finish_stencil_session(context, scene)
    except (ImportError, AttributeError, RuntimeError):
        pass
    settings.paint_enabled = False
    settings.paint_state = "EXITING"
    try:
        _restore_snapshot_and_keep_workspace(context, overlay)
        settings.active_paint_image = settings.overlay_image
        settings.paint_mask_mode = False
        _set_status(scene, "Texture Paint 종료", "READY", "IDLE")
    finally:
        settings.paint_snapshot_json = "{}"


def _paint_entry_dimensions(
    settings: Any,
    *,
    replace_existing_overlay: bool,
) -> tuple[int, int]:
    """Paint 진입 해상도를 읽고 확인되지 않은 기존 Layer 초기화를 차단합니다."""

    width, height = _selected_overlay_resolution(settings)
    change = pending_overlay_resolution_change(
        settings.paint_target,
        width,
        height,
        use_base_resolution=bool(settings.use_base_resolution),
    )
    if change is not None and not bool(replace_existing_overlay):
        current, desired = change
        raise PaintSessionError(
            f"기존 {_resolution_label(current)} Paint 이미지가 있습니다. "
            f"Painting Mode 버튼에서 {_resolution_label(desired)} 전체 Layer "
            "초기화를 확인하십시오."
        )
    return width, height


def _activate_paint_operator_runtime(context: Any, session: PaintSession) -> None:
    """준비된 Paint Session의 작업공간·Mode·Stencil 연결을 Main Thread에 예약합니다."""
    _set_status(context.scene, "Texture Paint 화면 연결 중", "RUNNING", "ENTERING")
    _schedule_paint_entry(context, session)


def _paint_progress_overlay_view(session: Any) -> ProgressOverlayView:
    """현재 Paint 준비 작업을 공통 중앙 Overlay 표시값으로 변환합니다."""

    progress = session.job.progress
    total = int(session.job.progress_total_units)
    completed = min(max(0, int(progress.completed_units)), total)
    units_text = f"{completed:,} / {total:,}" if total > 0 else "계산 중"
    return ProgressOverlayView(
        title=str(session.job.progress_title),
        phase=str(progress.phase),
        fraction=float(completed) / float(total),
        completed_units=completed if total else 0,
        total_units=total,
        elapsed_seconds=session.elapsed_seconds,
        detail_lines=(
            f"진행: {units_text}    경과: {session.elapsed_seconds:.1f}초",
            str(progress.detail),
            "변환 중에는 Blender 입력이 잠깁니다.",
        ),
    )


def _draw_paint_progress_overlay() -> None:
    """활성 Paint 준비 세션의 중앙 진행창을 그립니다."""

    session = active_paint_progress_session()
    if session is not None:
        draw_progress_overlay(_paint_progress_overlay_view(session))


def _continue_paint_after_conversion(job_id: str) -> None:
    """Modal Handler가 끝난 다음 Tick에서 Texture Paint 전환과 Runtime을 완료합니다."""

    session = get_paint_progress_session(job_id)
    if session is None:
        return None
    settings = getattr(session.scene, "projection_paint", None)
    try:
        while not session.job.done:
            session.job.advance()
        paint_session = session.job.result
        if paint_session is None:
            raise PaintSessionError("완료된 Texture Paint 진입 결과가 없습니다.")
        _activate_paint_operator_runtime(bpy.context, paint_session)
    except Exception as exc:
        try:
            release_paint_progress_session(session.job_id)
        except Exception as cleanup_exc:
            exc = RuntimeError(f"{exc} | 정리 실패: {cleanup_exc}")
        if settings is not None:
            cleanup_error = _rollback_failed_paint_entry(bpy.context, session.scene)
            if cleanup_error:
                exc = RuntimeError(f"{exc} | 정리 실패: {cleanup_error}")
        if settings is not None:
            settings.status = f"Texture Paint 진입 실패: {exc}"
            settings.status_kind = "ERROR"
            settings.paint_state = "ERROR"
        return None
    release_paint_progress_session(session.job_id)
    return None


def _schedule_paint_after_conversion(job_id: str) -> None:
    """Paint 준비 진행창이 닫힌 뒤 진입을 실행할 Main Thread Timer를 등록합니다."""

    from .lifecycle import lifecycle_registry

    def continue_on_next_tick() -> None:
        """원래 Modal 호출 스택이 끝난 뒤 보관한 Paint 작업을 이어서 실행합니다."""

        return _continue_paint_after_conversion(job_id)

    lifecycle_registry().register_timer(
        continue_on_next_tick,
        component="paint_conversion_finish",
        first_interval=0.0,
    )


class PP_OT_enter_paint(bpy.types.Operator):
    """선택한 Target으로 Texture Paint 자동화를 시작합니다."""

    bl_idname = "projection_paint.enter_paint"
    bl_label = "Enter Paint Mode"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    replace_existing_overlay: BoolProperty(
        name="전체 Layer 초기화",
        description="기존 Layer·Mask·보정 작업을 모두 삭제하고 새 Layer 1을 연결합니다",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    def _start_error(self, context: Any, exc: Exception) -> set[str]:
        """Paint 진입 시작 실패를 Scene 상태와 Operator Report에 기록합니다."""

        settings = context.scene.projection_paint
        settings.paint_enabled = False
        settings.status = str(exc)
        settings.status_kind = "ERROR"
        settings.paint_state = "ERROR"
        self.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    def invoke(self, context: Any, event: Any) -> set[str]:
        """최초 Paint 이미지 준비와 해상도 초기화는 중앙 진행창으로 시작합니다."""

        del event
        settings = context.scene.projection_paint
        try:
            validate_paint_target(settings.paint_target)
        except (PaintSetupError, RuntimeError, ValueError) as exc:
            return self._start_error(context, exc)
        if not _paint_progress_ui_available(context):
            return self.execute(context)
        try:
            recovery_overlay = _paint_entry_overlay_for_target(
                settings,
                settings.paint_target,
            )
            recovery_required = managed_overlay_recovery_required(
                settings.paint_target,
                recovery_overlay,
            )
            if not (
                bool(self.replace_existing_overlay)
                or requires_udim_conversion(settings.paint_target)
                or recovery_required
            ):
                return self.execute(context)
            width, height = _paint_entry_dimensions(
                settings,
                replace_existing_overlay=bool(self.replace_existing_overlay),
            )
            job = create_paint_session_start_job(
                context,
                settings.paint_target,
                width=width,
                height=height,
                use_base_resolution=bool(settings.use_base_resolution),
                replace_existing_overlay=bool(self.replace_existing_overlay),
            )
            session = create_paint_progress_session(context.scene, job)
        except (PaintSessionError, PaintSetupError, RuntimeError, ValueError) as exc:
            return self._start_error(context, exc)
        self.job_id = session.job_id
        overlay_handle = None
        timer = None
        try:
            overlay_handle = add_progress_overlay_draw_handler(
                _draw_paint_progress_overlay
            )
            timer = context.window_manager.event_timer_add(
                _PAINT_PROGRESS_TIMER_INTERVAL_SECONDS,
                window=context.window,
            )
            attach_paint_progress_ui(
                session.job_id,
                timer=timer,
                window_manager=context.window_manager,
                overlay_handle=overlay_handle,
            )
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            attached = get_paint_progress_session(session.job_id)
            if attached is not None and attached.timer is not None:
                try:
                    release_paint_progress_session(session.job_id)
                except Exception:
                    pass
            else:
                if timer is not None:
                    try:
                        context.window_manager.event_timer_remove(timer)
                    except (ReferenceError, RuntimeError, ValueError):
                        pass
                remove_progress_overlay_draw_handler(overlay_handle)
                try:
                    release_paint_progress_session(session.job_id)
                except Exception:
                    pass
            return self._start_error(
                context,
                RuntimeError(f"Paint 준비 진행 UI 시작 실패: {exc}"),
            )
        context.scene.projection_paint.status = (
            "해상도 초기화를 준비하고 있습니다."
            if bool(self.replace_existing_overlay)
            else "Paint 이미지를 준비하고 있습니다."
        )
        context.scene.projection_paint.status_kind = "RUNNING"
        context.scene.projection_paint.paint_state = "ENTERING"
        request_progress_overlay_redraw()
        return {"RUNNING_MODAL"}

    def execute(self, context):
        """Scene 설정의 Target과 해상도로 Paint와 Stencil을 시작합니다."""
        settings = context.scene.projection_paint
        try:
            validate_paint_target(settings.paint_target)
            width, height = _paint_entry_dimensions(
                settings,
                replace_existing_overlay=bool(self.replace_existing_overlay),
            )
            session = start_paint_session(
                context,
                settings.paint_target,
                width=width,
                height=height,
                use_base_resolution=bool(settings.use_base_resolution),
                replace_existing_overlay=bool(self.replace_existing_overlay),
            )
            _activate_paint_operator_runtime(context, session)
        except (
            AttributeError,
            PaintSessionError,
            PaintSetupError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            _rollback_failed_paint_entry(context, context.scene)
            return self._start_error(context, exc)
        return {"FINISHED"}

    def _fail(self, context: Any, session: Any, exc: Exception) -> set[str]:
        """Paint 준비 실패를 Rollback하고 Scene 상태와 Report에 기록합니다."""

        cleanup_message = ""
        try:
            release_paint_progress_session(session.job_id)
        except Exception as cleanup_exc:
            cleanup_message = f" | 정리 실패: {cleanup_exc}"
        settings = context.scene.projection_paint
        rollback_error = _rollback_failed_paint_entry(context, context.scene)
        if rollback_error:
            cleanup_message = f"{cleanup_message} | 상태 복구 실패: {rollback_error}"
        settings.status = f"Paint 이미지 준비 실패: {exc}{cleanup_message}"
        settings.status_kind = "ERROR"
        settings.paint_state = "ERROR"
        self.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    def _handoff(self, context: Any, session: Any) -> set[str]:
        """100% 화면 뒤 Modal을 닫고 다음 Tick의 Paint 진입으로 작업을 넘깁니다."""

        if not session.final_screen_shown:
            session.final_screen_shown = True
            request_progress_overlay_redraw()
            return {"RUNNING_MODAL"}
        try:
            detach_paint_progress_ui(session.job_id)
            _schedule_paint_after_conversion(session.job_id)
        except Exception as exc:
            return self._fail(context, session, exc)
        self.report({"INFO"}, "Paint 자원 준비가 완료되어 Texture Paint를 연결합니다.")
        return {"FINISHED"}

    def modal(self, context: Any, event: Any) -> set[str]:
        """Timer Event마다 실제 Paint 준비 단계 하나를 진행하고 Overlay를 갱신합니다."""

        session = get_paint_progress_session(self.job_id)
        if session is None:
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        if session.job.conversion_ready:
            return self._handoff(context, session)
        try:
            session.job.advance()
        except (
            AttributeError,
            PaintSessionError,
            PaintSetupError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return self._fail(context, session, exc)
        request_progress_overlay_redraw()
        if session.job.conversion_ready:
            return self._handoff(context, session)
        if session.job.done:
            release_paint_progress_session(session.job_id)
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def cancel(self, context: Any) -> None:
        """Blender 강제 종료 시 Paint 준비 작업과 UI 자원을 회수합니다."""

        session = get_paint_progress_session(self.job_id)
        if session is None:
            return
        try:
            release_paint_progress_session(session.job_id, reset_status=True)
        except Exception as exc:
            settings = context.scene.projection_paint
            settings.status = f"Paint 준비 강제 종료 정리에 실패했습니다: {exc}"
            settings.status_kind = "ERROR"
            settings.paint_state = "ERROR"


class PP_OT_exit_paint(bpy.types.Operator):
    """활성 Texture Paint 자동화를 안전하게 종료합니다."""

    bl_idname = "projection_paint.exit_paint"
    bl_label = "Exit Paint Mode"
    bl_options = {"REGISTER"}

    def execute(self, context):
        """영구 Overlay를 남기고 진입 전 사용자 상태를 복구합니다."""
        try:
            finish_paint_session(context)
        except (PaintSessionError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class PP_OT_toggle_paint(bpy.types.Operator):
    """단일 버튼으로 Texture Paint 자동화를 시작하거나 종료합니다."""

    bl_idname = "projection_paint.toggle_paint"
    bl_label = "Painting Mode"
    bl_description = "Projection Painting Mode를 켜거나 끕니다"
    bl_options = {"REGISTER"}

    draw = draw_wrapped_confirmation

    replace_existing_overlay: BoolProperty(
        name="전체 Layer 초기화 확인",
        description="확인 후 기존 Layer·Mask·보정 작업을 모두 삭제합니다",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    invoke_progress_overlay: BoolProperty(
        name="Paint 준비 진행창 사용",
        description="UI에서 시작한 Paint 이미지 준비에 중앙 진행창을 표시합니다",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: Any) -> bool:
        """활성 Paint 종료 또는 PP Snapshot이 준비된 신규 진입만 허용합니다."""

        from .snapshot_update import snapshot_is_ready

        scene = getattr(context, "scene", None)
        settings = getattr(scene, "projection_paint", None)
        return bool(
            settings is not None
            and (
                paint_runtime_is_active(scene)
                or snapshot_is_ready(scene)
            )
        )

    def invoke(self, context, event):
        """해상도 교체가 필요하면 기존 작업 폐기 경고창을 먼저 표시합니다."""
        settings = context.scene.projection_paint
        if paint_runtime_is_active(context.scene):
            return self.execute(context)
        try:
            validate_paint_target(settings.paint_target)
            width, height = _selected_overlay_resolution(settings)
            change = pending_overlay_resolution_change(
                settings.paint_target,
                width,
                height,
                use_base_resolution=bool(settings.use_base_resolution),
            )
        except (PaintSetupError, PaintSessionError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if change is None:
            return bpy.ops.projection_paint.enter_paint(
                "INVOKE_DEFAULT",
                replace_existing_overlay=False,
            )
        current, desired = change
        self.replace_existing_overlay = True
        self.invoke_progress_overlay = True
        return invoke_wrapped_confirmation(
            self,
            context,
            title="Paint 해상도 및 Layer 초기화",
            message=_resolution_reset_message(current, desired),
            confirm_text=f"전체 초기화 후 {_resolution_label(desired)} 생성",
            icon="STATUS_WARNING",
        )

    def execute(self, context):
        """현재 Paint 활성 상태에 맞는 기존 진입 또는 종료 Operator를 실행합니다."""
        if paint_runtime_is_active(context.scene):
            return bpy.ops.projection_paint.exit_paint()
        settings = context.scene.projection_paint
        try:
            validate_paint_target(settings.paint_target)
            width, height = _selected_overlay_resolution(settings)
            change = pending_overlay_resolution_change(
                settings.paint_target,
                width,
                height,
                use_base_resolution=bool(settings.use_base_resolution),
            )
        except (PaintSetupError, PaintSessionError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if change is not None and not bool(self.replace_existing_overlay):
            current, desired = change
            self.report(
                {"WARNING"},
                (
                    f"기존 {_resolution_label(current)} 작업을 보호했습니다. "
                    f"{_resolution_label(desired)} 전체 Layer 초기화 경고창에서 확인하십시오."
                ),
            )
            return {"CANCELLED"}
        if bool(self.invoke_progress_overlay) and context.window is not None:
            return bpy.ops.projection_paint.enter_paint(
                "INVOKE_DEFAULT",
                replace_existing_overlay=bool(self.replace_existing_overlay),
            )
        return bpy.ops.projection_paint.enter_paint(
            replace_existing_overlay=bool(self.replace_existing_overlay),
        )


CLASSES = (PP_OT_enter_paint, PP_OT_exit_paint, PP_OT_toggle_paint)


__all__ = [
    "CLASSES",
    "PaintSession",
    "PaintSessionError",
    "PaintSessionProgress",
    "PaintSessionStartJob",
    "active_paint_session",
    "create_paint_session_start_job",
    "finish_paint_session",
    "start_paint_session",
    "validate_active_paint_session",
]
