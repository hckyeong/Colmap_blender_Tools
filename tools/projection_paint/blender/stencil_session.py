"""Blender Scene Pointer를 권위로 Camera Image Stencil을 연결합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

import bpy
from bpy_extras import view3d_utils

from .ownership import PROJECT_STENCIL_DRAW_BRUSH_ROLE, is_owned, tag_owned
from .paint_state import resolve_stencil_texture, validate_paint_contract
from .paint_tool_bridge import (
    BrushAssetChoice,
    activate_brush_asset,
    active_brush_choice,
)


TEXTURE_ROLE = "STENCIL_TEXTURE"
PROJECT_STENCIL_BRUSH_NAME = "Projection Paint Stencil Draw"


class StencilSessionError(RuntimeError):
    """Stencil 자원 또는 Camera Frame 계약 오류를 나타냅니다."""


def _pointer(value: Any) -> int:
    """삭제된 Blender ID를 안전하게 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class StencilConnectionStatus:
    """현재 Blender Pointer에서 계산한 Stencil 연결 상태와 안내 문구를 보관합니다."""

    state: str
    reason: str
    bound_brush: Any | None
    active_brush: Any | None


@dataclass(slots=True)
class StencilSession:
    """호출 시점의 Blender Stencil Pointer를 편의상 조회하는 비권위 Snapshot입니다."""

    scene: Any
    window: Any | None = None
    area: Any | None = None
    region: Any | None = None
    space: Any | None = None
    rv3d: Any | None = None

    @property
    def brush(self):
        """Scene에 저장된 현재 Stencil Brush를 반환합니다."""
        return self.scene.projection_paint.stencil_brush

    @property
    def managed_texture(self):
        """Scene에 저장된 관리 Stencil Texture를 반환합니다."""
        return self.scene.projection_paint.stencil_texture

    @property
    def source_image(self):
        """Scene에 저장된 현재 Source Image를 반환합니다."""
        return self.scene.projection_paint.stencil_source_image

    @property
    def source_image_id(self) -> int | None:
        """Scene에 저장된 현재 IMAGE_ID를 반환합니다."""
        value = int(self.scene.projection_paint.active_image_id)
        return value if value > 0 else None

    @property
    def camera(self):
        """현재 Scene Camera를 반환합니다."""
        return self.scene.camera

    @property
    def manual_disabled(self) -> bool:
        """사용자의 수동 Stencil 해제 상태를 반환합니다."""
        return bool(self.scene.projection_paint.manual_stencil_disabled)

    @manual_disabled.setter
    def manual_disabled(self, value: bool) -> None:
        """사용자의 수동 Stencil 해제 상태를 Scene에 기록합니다."""
        self.scene.projection_paint.manual_stencil_disabled = bool(value)

    @property
    def outside_camera_view(self) -> bool:
        """Camera View 이탈 상태를 반환합니다."""
        return bool(self.scene.projection_paint.stencil_outside_camera_view)

    @outside_camera_view.setter
    def outside_camera_view(self, value: bool) -> None:
        """Camera View 이탈 상태를 Scene에 기록합니다."""
        self.scene.projection_paint.stencil_outside_camera_view = bool(value)

    def validate_resources(self) -> None:
        """Scene의 Paint Material과 관리 Texture 자원만 검증합니다."""
        validate_paint_contract(self.scene)
        if resolve_stencil_texture(self.scene) is None:
            raise StencilSessionError("관리 Stencil Texture가 삭제되거나 변경되었습니다.")

    def validate_binding(self) -> None:
        """기존에 명시적으로 연결한 Draw Brush Pointer를 검증합니다."""
        self.validate_resources()
        if _pointer(self.brush) == 0:
            raise StencilSessionError("Reconnect로 연결한 Draw Brush가 없습니다.")

    def validate(self) -> None:
        """호환 호출에서 기존 연결 Brush까지 포함한 계약을 검증합니다."""
        self.validate_binding()


def camera_frame_stencil(
    region_corners: tuple[tuple[float, float], ...],
    image_width: int,
    image_height: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Camera Frame 네 모서리를 Blender Stencil 중심과 반경으로 변환합니다."""
    if image_width <= 0 or image_height <= 0:
        raise StencilSessionError("Source Image 크기는 0보다 커야 합니다.")
    if len(region_corners) != 4:
        raise StencilSessionError("Camera Frame의 네 모서리가 모두 필요합니다.")
    corners = tuple((float(corner[0]), float(corner[1])) for corner in region_corners)
    if not all(math.isfinite(value) for corner in corners for value in corner):
        raise StencilSessionError("Camera Frame 좌표가 유효하지 않습니다.")
    minimum_x = min(corner[0] for corner in corners)
    maximum_x = max(corner[0] for corner in corners)
    minimum_y = min(corner[1] for corner in corners)
    maximum_y = max(corner[1] for corner in corners)
    frame_width = maximum_x - minimum_x
    frame_height = maximum_y - minimum_y
    if frame_width <= 0.0 or frame_height <= 0.0:
        raise StencilSessionError("Camera Frame의 화면 크기가 유효하지 않습니다.")
    frame_aspect = frame_width / frame_height
    image_aspect = float(image_width) / float(image_height)
    if not math.isclose(frame_aspect, image_aspect, rel_tol=1.0e-3, abs_tol=1.0e-3):
        raise StencilSessionError("Camera Frame과 Source Image의 종횡비가 일치하지 않습니다.")
    return (
        ((minimum_x + maximum_x) * 0.5, (minimum_y + maximum_y) * 0.5),
        (frame_width * 0.5, frame_height * 0.5),
    )


def _viewport_parts(context: Any):
    """현재 Window에서 사용할 첫 3D Viewport 구성요소를 반환합니다."""
    area = getattr(context, "area", None)
    window = getattr(context, "window", None)
    try:
        current_is_view = bool(
            window is not None
            and area is not None
            and area.type == "VIEW_3D"
            and area in tuple(window.screen.areas)
        )
    except (AttributeError, ReferenceError, RuntimeError):
        current_is_view = False
    if window is not None and not current_is_view:
        area = next(
            (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
            None,
        )
    region = next(
        (item for item in getattr(area, "regions", ()) if item.type == "WINDOW"),
        None,
    )
    space = getattr(getattr(area, "spaces", None), "active", None)
    if (
        window is None
        or area is None
        or area.type != "VIEW_3D"
        or region is None
        or space is None
        or space.type != "VIEW_3D"
    ):
        return None, None, None, None, None
    return window, area, region, space, space.region_3d


def _snapshot(context: Any, scene: Any | None = None) -> StencilSession:
    """현재 Context와 Scene Pointer에서 비권위 Stencil Snapshot을 생성합니다."""
    active_scene = scene if scene is not None else getattr(context, "scene", None)
    if active_scene is None:
        raise StencilSessionError("Stencil을 연결할 Scene이 없습니다.")
    window, area, region, space, rv3d = _viewport_parts(context)
    return StencilSession(active_scene, window, area, region, space, rv3d)


def active_stencil_session(scene: Any | None = None) -> StencilSession | None:
    """Blender Pointer가 유효할 때 현재 Stencil Snapshot을 반환합니다."""
    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    if active_scene is None or resolve_stencil_texture(active_scene) is None:
        return None
    return _snapshot(bpy.context, active_scene)


def active_image_paint_tool(context: Any):
    """현재 Workspace의 Texture Paint 도구를 변경 없이 조회합니다."""

    workspace = getattr(context, "workspace", None)
    if workspace is None:
        workspace = getattr(getattr(context, "window", None), "workspace", None)
    tools = getattr(workspace, "tools", None)
    if tools is None:
        return None
    try:
        return tools.from_space_view3d_mode("PAINT_TEXTURE", create=False)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None


def active_draw_brush_for_reconnect(context: Any, scene: Any | None = None):
    """실제 Workspace Tool과 활성 Brush가 모두 Draw인지 검증해 반환합니다."""

    active_scene = scene if scene is not None else getattr(context, "scene", None)
    image_paint = getattr(
        getattr(active_scene, "tool_settings", None),
        "image_paint",
        None,
    )
    tool = active_image_paint_tool(context)
    brush = getattr(image_paint, "brush", None)
    tool_id = str(getattr(tool, "idname", "") or "")
    tool_type = str(getattr(tool, "brush_type", "") or "")
    brush_type = str(getattr(brush, "image_brush_type", "") or "")
    draw_tool = tool_type == "DRAW" or tool_id == "builtin.brush"
    if tool is None or not draw_tool or _pointer(brush) == 0 or brush_type != "DRAW":
        raise StencilSessionError(
            "Blender의 Draw Brush 도구를 선택한 뒤 Reconnect를 실행하십시오."
        )
    return tool, brush


def resolve_stencil_connection(scene: Any, context: Any = None) -> StencilConnectionStatus:
    """저장 상태보다 현재 Texture·Bound Brush·Active Brush Pointer를 우선 판정합니다."""

    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return StencilConnectionStatus(
            "DISCONNECTED",
            "Projection Paint 설정이 없습니다.",
            None,
            None,
        )
    image_paint = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    active = getattr(image_paint, "brush", None)
    bound = getattr(settings, "stencil_brush", None)
    texture = resolve_stencil_texture(scene)
    if bool(settings.paint_mask_mode):
        status = StencilConnectionStatus(
            "MASK_SUSPENDED",
            "Projection Paint Mask 편집 중입니다. 일반 Layer에서 다시 연결하십시오.",
            bound if _pointer(bound) else None,
            active if _pointer(active) else None,
        )
    elif _pointer(bound) == 0:
        status = StencilConnectionStatus(
            "DISCONNECTED",
            "Draw Brush를 선택하고 Reconnect를 실행하십시오.",
            None,
            active if _pointer(active) else None,
        )
    elif active is not bound:
        status = StencilConnectionStatus(
            "BRUSH_CHANGED",
            "다른 Brush가 활성 상태입니다. 연결한 Draw Brush로 돌아가면 Stencil이 유지됩니다.",
            bound,
            active if _pointer(active) else None,
        )
    elif bool(settings.manual_stencil_disabled):
        status = StencilConnectionStatus(
            "MANUAL_DISABLED",
            "Stencil 연결이 수동으로 해제되었습니다. Reconnect를 실행하십시오.",
            bound,
            active,
        )
    elif bool(settings.stencil_outside_camera_view):
        status = StencilConnectionStatus(
            "CAMERA_SUSPENDED",
            "Camera View로 돌아가면 같은 Brush 연결을 복구합니다.",
            bound,
            active,
        )
    elif texture is None:
        status = StencilConnectionStatus(
            "ERROR",
            "관리 Stencil Texture가 삭제되거나 변경되었습니다.",
            bound,
            active,
        )
    elif _pointer(settings.stencil_source_image) == 0 or int(settings.active_image_id) <= 0:
        status = StencilConnectionStatus(
            "DISCONNECTED",
            "현재 Camera Frame Source가 준비되지 않았습니다.",
            bound,
            active,
        )
    elif bound.texture is texture and str(bound.texture_slot.map_mode) == "STENCIL":
        status = StencilConnectionStatus("CONNECTED", "", bound, active)
    else:
        status = StencilConnectionStatus(
            "DISCONNECTED",
            "기존 Draw Brush의 Stencil 연결이 해제되었습니다. Reconnect를 실행하십시오.",
            bound,
            active,
        )
    try:
        settings.stencil_state = status.state
        settings.stencil_reason = status.reason
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    return status


def stencil_connection_allowed(scene: Any) -> bool:
    """일반 Layer가 활성이고 Scene Camera가 연결된 경우에만 Stencil 연결을 허용합니다."""
    settings = getattr(scene, "projection_paint", None)
    camera = getattr(scene, "camera", None)
    return bool(
        settings is not None
        and not bool(settings.paint_mask_mode)
        and camera is not None
        and _pointer(camera)
        and getattr(camera, "data", None) is not None
    )


def project_camera_frame(session: StencilSession, camera) -> tuple[tuple[float, float], ...]:
    """현재 Snapshot Viewport에서 Camera Frame 네 모서리를 투영합니다."""
    if (
        session.region is None
        or session.rv3d is None
        or _pointer(camera) == 0
        or getattr(camera, "data", None) is None
    ):
        raise StencilSessionError("Camera Frame을 투영할 3D Viewport가 없습니다.")
    try:
        session.rv3d.update()
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    corners = []
    for local_corner in camera.data.view_frame(scene=session.scene):
        point = view3d_utils.location_3d_to_region_2d(
            session.region,
            session.rv3d,
            camera.matrix_world @ local_corner,
        )
        if point is None:
            raise StencilSessionError("Camera Frame을 Viewport 좌표로 투영하지 못했습니다.")
        corners.append((float(point.x), float(point.y)))
    return tuple(corners)


def _new_managed_texture(dataset_uuid: str, source_fingerprint: str):
    """Source Image를 담을 관리 Image Texture를 생성합니다."""
    texture = bpy.data.textures.new(
        name=f"Projection Stencil Texture {dataset_uuid[:8]}",
        type="IMAGE",
    )
    tag_owned(
        texture,
        TEXTURE_ROLE,
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
    )
    return texture


def ensure_stencil_texture(
    scene: Any,
    *,
    dataset_uuid: str = "",
    source_fingerprint: str = "",
):
    """Scene에 단일 관리 Stencil Texture가 존재하도록 보장합니다."""
    texture = resolve_stencil_texture(scene)
    if texture is not None:
        return texture
    texture = _new_managed_texture(dataset_uuid, source_fingerprint)
    scene.projection_paint.stencil_texture = texture
    return texture


def _project_stencil_brush_is_valid(brush: Any) -> bool:
    """Brush가 저장 가능한 프로젝트 전용 Draw Brush인지 확인합니다."""

    return bool(
        _pointer(brush)
        and is_owned(brush, kind=PROJECT_STENCIL_DRAW_BRUSH_ROLE)
        and getattr(brush, "library", None) is None
        and bool(getattr(brush, "is_editable", False))
        and str(getattr(brush, "image_brush_type", "") or "") == "DRAW"
        and getattr(brush, "asset_data", None) is not None
    )


def _activate_project_stencil_brush(context: Any, scene: Any, brush: Any):
    """저장된 프로젝트 전용 Draw Brush Asset을 현재 Draw Tool에 활성화합니다."""

    active = activate_brush_asset(
        context,
        scene,
        BrushAssetChoice("LOCAL", "", f"Brush/{brush.name}"),
        expected_tool_id="builtin.brush",
        expected_brush_type="DRAW",
    )
    if active is not brush:
        raise StencilSessionError("프로젝트 전용 Draw Brush Pointer가 일치하지 않습니다.")
    return active


def _create_project_stencil_brush(context: Any, scene: Any, source: Any):
    """현재 Draw 설정을 복제한 프로젝트 전용 Stencil Brush를 한 번 생성합니다."""

    source_choice = active_brush_choice(scene)
    brush = source.copy()
    brush.name = PROJECT_STENCIL_BRUSH_NAME
    if getattr(brush, "asset_data", None) is None:
        brush.asset_mark()
    tag_owned(
        brush,
        PROJECT_STENCIL_DRAW_BRUSH_ROLE,
        dataset_uuid=str(scene.projection_paint.dataset_uuid or ""),
    )
    _configure_project_stencil_brush(brush)
    try:
        return _activate_project_stencil_brush(context, scene, brush)
    except Exception:
        try:
            if source_choice is not None:
                activate_brush_asset(
                    context,
                    scene,
                    source_choice,
                    expected_tool_id="builtin.brush",
                    expected_brush_type="DRAW",
                )
            if scene.tool_settings.image_paint.brush is not brush:
                bpy.data.brushes.remove(brush)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
        raise


def _configure_project_stencil_brush(brush: Any) -> None:
    """Camera Texture 원색 투영을 위해 전용 Draw Brush 색상을 흰색으로 맞춥니다."""

    brush.color = (1.0, 1.0, 1.0)


def bind_active_draw_brush(context: Any, scene: Any = None):
    """명시 Reconnect에서 프로젝트 전용 Draw Brush를 생성하거나 다시 활성화합니다."""

    active_scene = scene if scene is not None else getattr(context, "scene", None)
    if active_scene is None:
        raise StencilSessionError("Stencil을 연결할 Scene이 없습니다.")
    _tool, active = active_draw_brush_for_reconnect(context, active_scene)
    settings = active_scene.projection_paint
    brush = settings.stencil_brush
    if not _project_stencil_brush_is_valid(brush):
        brush = active if _project_stencil_brush_is_valid(active) else None
    if brush is None:
        brush = _create_project_stencil_brush(context, active_scene, active)
    elif active is not brush:
        brush = _activate_project_stencil_brush(context, active_scene, brush)
    _configure_project_stencil_brush(brush)
    settings.stencil_brush = brush
    settings.manual_stencil_disabled = False
    settings.stencil_outside_camera_view = False
    resolve_stencil_connection(active_scene, context)
    return brush


def start_stencil_session(
    context: Any,
    _paint_token: Any = None,
    *,
    dataset_uuid: str,
    source_fingerprint: str = "",
    brush_override: Any = None,
) -> StencilSession:
    """Paint 진입에서 Brush를 변경하지 않고 단일 Stencil Texture 자원만 준비합니다."""
    scene = context.scene
    validate_paint_contract(scene)
    settings = scene.projection_paint
    ensure_stencil_texture(
        scene,
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
    )
    if brush_override is not None:
        raise StencilSessionError(
            "Paint 진입에서는 Brush를 연결할 수 없습니다. Reconnect를 실행하십시오."
        )
    settings.manual_stencil_disabled = False
    settings.stencil_outside_camera_view = False
    session = _snapshot(context, scene)
    session.validate_resources()
    resolve_stencil_connection(scene, context)
    return session


def stage_stencil_source(
    session: StencilSession,
    *,
    image: Any,
    image_id: int,
    camera: Any = None,
    frame_corners: tuple[tuple[float, float], ...] | None = None,
) -> None:
    """현재 Frame Source와 Camera를 Blender Pointer 상태에 기록합니다."""
    session.validate_resources()
    from .camera_scene import (
        apply_projection_camera_state,
        projection_camera_session_is_active,
    )

    if not projection_camera_session_is_active(session.scene):
        raise StencilSessionError("활성 Projection Paint Camera 세션이 아닙니다.")
    if image is None or _pointer(image) == 0 or min(int(value) for value in image.size) <= 0:
        raise StencilSessionError("현재 Frame Source Image가 유효하지 않습니다.")
    settings = session.scene.projection_paint
    settings.stencil_source_image = image
    settings.active_image_id = int(image_id)
    settings.current_image_id = str(int(image_id))
    settings.current_image_name = str(image.name)
    settings.stencil_frame_corners_json = (
        json.dumps(frame_corners) if frame_corners is not None else ""
    )
    texture = ensure_stencil_texture(session.scene)
    texture.image = image
    if camera is not None and _pointer(camera):
        apply_projection_camera_state(session.scene, camera)


def bind_stencil_source(
    context: Any,
    scene: Any,
    image: Any,
    image_id: int,
    camera: Any,
) -> None:
    """현재 IMAGE_ID Source를 Scene Pointer와 관리 Texture에 연결합니다."""
    stage_stencil_source(
        _snapshot(context, scene),
        image=image,
        image_id=image_id,
        camera=camera,
    )


def _stored_frame_corners(scene: Any):
    """테스트 또는 고정 Frame 정렬용 저장 모서리를 반환합니다."""
    payload = str(scene.projection_paint.stencil_frame_corners_json or "")
    if not payload:
        return None
    try:
        values = json.loads(payload)
        corners = tuple((float(value[0]), float(value[1])) for value in values)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StencilSessionError("저장된 Camera Frame 모서리가 손상되었습니다.") from exc
    return corners


def _enter_camera_view(session: StencilSession) -> None:
    """현재 카메라뷰를 확정하고 이탈 시 원근투영 복귀 상태를 기록합니다."""
    from .camera_scene import enter_projection_camera_view

    camera = session.scene.camera
    if camera is None or _pointer(camera) == 0 or getattr(camera, "data", None) is None:
        raise StencilSessionError("Stencil을 연결할 Scene Camera가 없습니다.")
    if session.space is not None and _pointer(session.space):
        session.space.use_local_camera = True
        session.space.camera = camera
    if session.rv3d is not None and _pointer(session.rv3d):
        enter_projection_camera_view(session.window, session.area, session.space)


def align_stencil_to_camera(context: Any, scene: Any) -> None:
    """현재 Camera Frame에 활성 Brush Stencil 위치와 크기를 맞춥니다."""
    session = _snapshot(context, scene)
    session.validate_binding()
    settings = scene.projection_paint
    image = session.source_image
    if image is None or _pointer(image) == 0:
        raise StencilSessionError("현재 Stencil Source Image가 삭제되었습니다.")
    camera = scene.camera
    corners = _stored_frame_corners(scene) or project_camera_frame(session, camera)
    position, dimensions = camera_frame_stencil(
        corners,
        int(image.size[0]),
        int(image.size[1]),
    )
    texture = session.managed_texture
    brush = session.brush
    if _project_stencil_brush_is_valid(brush):
        _configure_project_stencil_brush(brush)
    texture.image = image
    brush.texture_slot.map_mode = "STENCIL"
    brush.stencil_pos = position
    brush.stencil_dimension = dimensions
    brush.texture = texture
    if brush.texture is not texture or brush.texture_slot.map_mode != "STENCIL":
        raise StencilSessionError("관리 Stencil Texture 연결을 확정하지 못했습니다.")


def disconnect_stencil(scene: Any) -> bool:
    """Source와 Paint 활성 상태를 유지한 채 기존 Brush Texture 연결만 해제합니다."""
    settings = scene.projection_paint
    brush = settings.stencil_brush
    texture = resolve_stencil_texture(scene)
    resources_ready = brush is not None and texture is not None
    if resources_ready and brush.texture is texture:
        brush.texture = None
    if not bool(settings.paint_mask_mode):
        settings.stencil_outside_camera_view = True
    resolve_stencil_connection(scene)
    return resources_ready


def disconnect_stencil_for_camera_exit(
    session: StencilSession | None = None,
) -> bool:
    """호환 호출에서 Camera View 이탈 연결 해제를 Scene 기반으로 실행합니다."""
    current = session if session is not None else active_stencil_session()
    return disconnect_stencil(current.scene) if current is not None else False


def reconnect_stencil_texture(
    context: Any,
    session: StencilSession | None = None,
) -> int:
    """명시 Reconnect에서 프로젝트 전용 Draw Brush에 STENCIL Mapping을 연결합니다."""
    current = session if session is not None else _snapshot(context)
    scene = current.scene
    settings = scene.projection_paint
    validate_paint_contract(scene)
    if bool(settings.paint_mask_mode):
        raise StencilSessionError("Mask 레이어에서는 Stencil을 연결할 수 없습니다.")
    if not stencil_connection_allowed(scene):
        raise StencilSessionError("Stencil을 연결할 Scene Camera가 없습니다.")
    if settings.stencil_source_image is None or int(settings.active_image_id) <= 0:
        raise StencilSessionError("현재 Frame Source가 준비되지 않았습니다.")
    active = scene.tool_settings.image_paint.brush
    brush = settings.stencil_brush
    if _pointer(brush) == 0:
        raise StencilSessionError("Draw Brush를 선택하고 Reconnect를 실행하십시오.")
    if active is not brush:
        raise StencilSessionError(
            "활성 Brush가 연결 Brush와 다릅니다. Draw Brush에서 Reconnect를 실행하십시오."
        )
    if not _project_stencil_brush_is_valid(brush):
        raise StencilSessionError(
            "프로젝트 전용 Draw Brush가 준비되지 않았습니다. Draw에서 Reconnect를 다시 실행하십시오."
        )
    ensure_stencil_texture(
        scene,
        dataset_uuid=str(settings.dataset_uuid),
        source_fingerprint=str(settings.source_fingerprint),
    )
    current = _snapshot(context, scene)
    _enter_camera_view(current)
    align_stencil_to_camera(context, scene)
    settings.manual_stencil_disabled = False
    settings.stencil_outside_camera_view = False
    resolve_stencil_connection(scene, context)
    return int(settings.active_image_id)


def refresh_stencil(
    context: Any,
    session: StencilSession | None = None,
    *,
    refresh_viewport: bool = True,
) -> bool:
    """자동 복구 없이 현재 Stencil 연결 여부만 확인합니다."""

    del refresh_viewport
    current = session if session is not None else _snapshot(context or bpy.context)
    return resolve_stencil_connection(current.scene, context).state == "CONNECTED"


def stencil_view_token(session: StencilSession) -> tuple[object, ...]:
    """저비용 비교용 Camera View·Stencil 연결 상태 Token을 반환합니다."""
    current = _snapshot(bpy.context, session.scene)
    current.validate_resources()
    region = current.region
    space = current.space
    rv3d = current.rv3d
    camera = current.scene.camera
    if (
        region is None
        or space is None
        or rv3d is None
        or _pointer(region) == 0
        or _pointer(space) == 0
        or _pointer(rv3d) == 0
    ):
        raise StencilSessionError("추적할 3D Viewport가 유효하지 않습니다.")
    camera_data = getattr(camera, "data", None)
    camera_matrix = (
        tuple(float(value) for row in camera.matrix_world for value in row)
        if camera is not None and _pointer(camera) and camera_data is not None
        else ()
    )
    brush = current.brush
    texture = current.managed_texture
    render = current.scene.render
    return (
        bool(
            stencil_connection_allowed(current.scene)
            and str(rv3d.view_perspective) == "CAMERA"
        ),
        _pointer(region),
        int(region.width),
        int(region.height),
        _pointer(space),
        _pointer(rv3d),
        str(rv3d.view_perspective),
        float(rv3d.view_camera_zoom),
        tuple(float(value) for value in rv3d.view_camera_offset),
        _pointer(getattr(space, "camera", None)),
        _pointer(camera),
        _pointer(camera_data),
        camera_matrix,
        float(getattr(camera_data, "lens", 0.0)),
        float(getattr(camera_data, "shift_x", 0.0)),
        float(getattr(camera_data, "shift_y", 0.0)),
        int(render.resolution_x),
        int(render.resolution_y),
        float(render.pixel_aspect_x),
        float(render.pixel_aspect_y),
        _pointer(current.scene.tool_settings.image_paint.brush),
        _pointer(getattr(brush, "texture", None)),
        str(getattr(getattr(brush, "texture_slot", None), "map_mode", "")),
        _pointer(getattr(texture, "image", None)),
    )


def detach_source_image(image: Any) -> None:
    """LRU 퇴거 전에 Scene Pointer가 가리키는 동일 Source 참조를 해제합니다."""
    if image is None:
        return
    for scene in bpy.data.scenes:
        settings = getattr(scene, "projection_paint", None)
        if settings is None or settings.stencil_source_image is not image:
            continue
        texture = resolve_stencil_texture(scene)
        brush = settings.stencil_brush
        if brush is not None and texture is not None and brush.texture is texture:
            brush.texture = None
        if texture is not None and texture.image is image:
            texture.image = None
        settings.stencil_source_image = None
        settings.active_image_id = 0


def finish_stencil_session(context: Any = None, scene: Any = None) -> None:
    """Paint 종료에서도 Blender가 저장한 Draw Brush Stencil 연결을 그대로 유지합니다."""
    active_context = context if context is not None else bpy.context
    active_scene = scene if scene is not None else getattr(active_context, "scene", None)
    if active_scene is None:
        return
    if getattr(active_scene, "projection_paint", None) is None:
        return
    resolve_stencil_connection(active_scene, active_context)


def project_current_stencil(context: Any) -> int:
    """현재 Frame Source를 활성 카메라에서 보이는 Target 표면 전체에 투영합니다."""
    scene = context.scene
    settings = scene.projection_paint
    if not bool(settings.paint_enabled):
        raise StencilSessionError("먼저 Painting Mode를 활성화하십시오.")
    if bool(settings.paint_mask_mode):
        raise StencilSessionError("Layer를 선택한 뒤 Stencil을 투영하십시오.")
    connection = resolve_stencil_connection(scene, context)
    if connection.state != "CONNECTED":
        raise StencilSessionError(
            "현재 Stencil이 연결되지 않았습니다. Draw Brush에서 Reconnect를 먼저 실행하십시오."
        )
    from .paint_runtime import reconnect_current_frame

    image_id = reconnect_current_frame(context)
    if resolve_stencil_connection(scene, context).state != "CONNECTED":
        raise StencilSessionError(
            "현재 Stencil 연결을 복구하지 못했습니다. Reconnect를 다시 실행하십시오."
        )
    session = active_stencil_session(scene)
    if session is None or session.source_image is None:
        raise StencilSessionError("현재 Frame의 Stencil Source Image가 없습니다.")
    target = settings.paint_target
    overlay = settings.active_paint_image or settings.overlay_image
    if target is None or _pointer(target) == 0 or target.mode != "TEXTURE_PAINT":
        raise StencilSessionError("Paint Target이 Texture Paint Mode가 아닙니다.")
    if overlay is None or _pointer(overlay) == 0:
        raise StencilSessionError("투영 결과를 기록할 Paint Overlay가 없습니다.")
    window, area, region, _space, rv3d = _viewport_parts(context)
    if window is None or area is None or region is None or rv3d is None:
        raise StencilSessionError("투영에 사용할 3D Viewport가 없습니다.")
    if str(rv3d.view_perspective) != "CAMERA":
        raise StencilSessionError("Camera View에서 Stencil을 투영하십시오.")
    with context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
        active_object=target,
        object=target,
    ):
        if not bpy.ops.paint.project_image.poll():
            raise StencilSessionError("현재 Texture Paint 상태에서는 카메라 투영을 실행할 수 없습니다.")
        result = bpy.ops.paint.project_image(image=session.source_image.name)
    if result != {"FINISHED"}:
        raise StencilSessionError(f"Stencil 카메라 투영에 실패했습니다: {result}")
    overlay.update()
    from .overlay_durability import mark_overlay_dirty

    mark_overlay_dirty(overlay)
    settings.durability_state = "DIRTY"
    settings.status = f"IMAGE_ID {image_id} Stencil을 Target에 투영했습니다."
    settings.status_kind = "SUCCESS"
    return image_id


class PP_OT_reconnect_stencil(bpy.types.Operator):
    """현재 Viewport 시점에 가장 적합한 Camera Source를 Stencil로 연결합니다."""

    bl_idname = "projection_paint.reconnect_stencil"
    bl_label = "Reconnect Stencil Texture"
    bl_description = "현재 시점에서 위치와 방향이 가장 가까운 COLMAP Camera와 Stencil을 연결합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: Any) -> bool:
        """실제 ACTIVE Paint Runtime과 일반 Layer가 준비됐을 때만 재연결을 허용합니다."""

        from .snapshot_update import snapshot_is_ready
        from .paint_runtime import active_paint_runtime

        scene = getattr(context, "scene", None)
        settings = getattr(scene, "projection_paint", None) if scene is not None else None
        window = getattr(context, "window", None)
        return bool(
            scene is not None
            and settings is not None
            and window is not None
            and getattr(window, "workspace", None)
            is getattr(settings, "paint_workspace", None)
            and not bool(settings.paint_mask_mode)
            and str(settings.paint_state) == "ACTIVE"
            and active_paint_runtime(scene) is not None
            and snapshot_is_ready(scene)
        )

    def execute(self, context):
        """KDB-tree로 Camera를 선택하고 해당 Frame의 STENCIL Mapping을 복구합니다."""
        try:
            from .paint_runtime import reconnect_nearest_camera

            candidate = reconnect_nearest_camera(context, bind_active_brush=True)
        except (StencilSessionError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            (
                f"IMAGE_ID {candidate.entry.image_id} · Frame {candidate.entry.frame} · "
                f"거리 {candidate.distance:.3f} · 시선 일치 {candidate.alignment:.3f}"
            ),
        )
        return {"FINISHED"}


class PP_OT_project_stencil(bpy.types.Operator):
    """현재 Frame Stencil을 Target에 카메라 기준으로 한 번에 투영합니다."""

    bl_idname = "projection_paint.project_stencil"
    bl_label = "Project Stencil to Target"
    bl_description = "현재 카메라에서 보이는 Target 표면에 Stencil Source 전체를 투영합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: Any) -> bool:
        """일반 Layer의 Painting Mode와 유효한 Target·Source에서만 활성화합니다."""
        from .snapshot_update import snapshot_is_ready
        from .paint_runtime import active_paint_runtime

        scene = getattr(context, "scene", None)
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and scene is not None
            and snapshot_is_ready(scene)
            and str(settings.paint_state) == "ACTIVE"
            and active_paint_runtime(scene) is not None
            and not bool(settings.paint_mask_mode)
            and settings.paint_target is not None
        )

    def execute(self, context):
        """현재 Stencil Source를 카메라 재투영하고 결과를 보고합니다."""
        try:
            image_id = project_current_stencil(context)
        except (StencilSessionError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"IMAGE_ID {image_id} Stencil을 Target에 투영했습니다.")
        return {"FINISHED"}


CLASSES = (PP_OT_reconnect_stencil, PP_OT_project_stencil)


__all__ = [
    "CLASSES",
    "StencilConnectionStatus",
    "StencilSession",
    "StencilSessionError",
    "active_draw_brush_for_reconnect",
    "active_image_paint_tool",
    "active_stencil_session",
    "align_stencil_to_camera",
    "bind_active_draw_brush",
    "bind_stencil_source",
    "camera_frame_stencil",
    "detach_source_image",
    "disconnect_stencil",
    "disconnect_stencil_for_camera_exit",
    "ensure_stencil_texture",
    "finish_stencil_session",
    "project_camera_frame",
    "project_current_stencil",
    "reconnect_stencil_texture",
    "refresh_stencil",
    "resolve_stencil_connection",
    "stage_stencil_source",
    "start_stencil_session",
    "stencil_connection_allowed",
    "stencil_view_token",
]
