"""Blender Scene Pointer를 권위로 Camera Image Stencil을 연결합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

import bpy
from bpy_extras import view3d_utils

from .ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY, tag_owned
from .paint_state import resolve_stencil_texture, validate_paint_contract


TEXTURE_ROLE = "STENCIL_TEXTURE"
BRUSH_ROLE = "STENCIL_BRUSH"


class StencilSessionError(RuntimeError):
    """Stencil 자원 또는 Camera Frame 계약 오류를 나타냅니다."""


def _pointer(value: Any) -> int:
    """삭제된 Blender ID를 안전하게 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class BrushAssetReference:
    """원래 Brush Asset을 다시 활성화할 경로를 보관합니다."""

    library_type: str
    library_identifier: str
    relative_identifier: str


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

    @property
    def follow_active_brush(self) -> bool:
        """활성 Brush 승계 정책을 반환합니다."""
        return bool(self.scene.projection_paint.stencil_follow_active_brush)

    def validate(self) -> None:
        """Scene의 Material·Brush·Texture 권위 Pointer를 검증합니다."""
        validate_paint_contract(self.scene)
        if _pointer(self.brush) == 0:
            raise StencilSessionError("활성 Texture Paint Brush가 없습니다.")
        if resolve_stencil_texture(self.scene) is None:
            raise StencilSessionError("관리 Stencil Texture가 삭제되거나 변경되었습니다.")


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


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    """경로가 지정한 상위 경로 안에 있는지 확인합니다."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _brush_asset_reference(brush: Any) -> BrushAssetReference:
    """연결된 Brush Asset을 다시 활성화할 식별자를 구성합니다."""
    library = getattr(brush, "library", None)
    if library is None:
        raise StencilSessionError("편집 불가 Brush의 원본 Asset Library가 없습니다.")
    library_path = Path(bpy.path.abspath(library.filepath)).resolve()
    version = f"{bpy.app.version[0]}.{bpy.app.version[1]}"
    essentials_root = (
        Path(bpy.app.binary_path).resolve().parent / version / "datafiles" / "assets"
    ).resolve()
    if _path_is_relative_to(library_path, essentials_root):
        relative_library = library_path.relative_to(essentials_root).as_posix()
        return BrushAssetReference(
            "ESSENTIALS",
            "",
            f"{relative_library}/Brush/{brush.name}",
        )
    for asset_library in bpy.context.preferences.filepaths.asset_libraries:
        root = Path(asset_library.path).expanduser().resolve()
        if _path_is_relative_to(library_path, root):
            relative_library = library_path.relative_to(root).as_posix()
            return BrushAssetReference(
                "CUSTOM",
                str(asset_library.name),
                f"{relative_library}/Brush/{brush.name}",
            )
    raise StencilSessionError("현재 Brush의 Asset Library를 식별하지 못했습니다.")


def _activate_brush_asset(context: Any, reference: BrushAssetReference):
    """보관한 Asset 식별자로 원래 Brush를 활성화합니다."""
    window, area, region, _space, _rv3d = _viewport_parts(context)
    if window is None or area is None or region is None:
        raise StencilSessionError("원래 Brush를 복구할 3D Viewport가 없습니다.")
    with context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.brush.asset_activate(
            asset_library_type=reference.library_type,
            asset_library_identifier=reference.library_identifier,
            relative_asset_identifier=reference.relative_identifier,
        )
    if result != {"FINISHED"}:
        raise StencilSessionError(f"원래 Brush Asset 활성화에 실패했습니다: {result}")
    brush = context.scene.tool_settings.image_paint.brush
    if brush is None or _pointer(brush) == 0:
        raise StencilSessionError("활성화된 원래 Brush를 찾지 못했습니다.")
    return brush


def _capture_brush_state(scene: Any, brush: Any) -> None:
    """현재 Brush의 Texture·색상·Stencil 배치를 Scene Property에 저장합니다."""
    if brush is None or _pointer(brush) == 0:
        raise StencilSessionError("활성 Texture Paint Brush가 없습니다.")
    settings = scene.projection_paint
    settings.original_brush_texture = brush.texture
    settings.original_brush_map_mode = str(brush.texture_slot.map_mode)
    settings.original_brush_color = tuple(float(value) for value in brush.color)
    settings.original_stencil_pos = tuple(float(value) for value in brush.stencil_pos)
    settings.original_stencil_dimension = tuple(
        float(value) for value in brush.stencil_dimension
    )
    settings.stencil_original_state_valid = True


def _restore_brush_state(scene: Any, brush: Any) -> None:
    """Scene에 저장된 사용자 Brush 상태를 해당 Brush에 복구합니다."""
    settings = scene.projection_paint
    if not bool(settings.stencil_original_state_valid) or _pointer(brush) == 0:
        return
    brush.texture = settings.original_brush_texture
    if settings.original_brush_map_mode:
        brush.texture_slot.map_mode = settings.original_brush_map_mode
    brush.color = tuple(settings.original_brush_color)
    brush.stencil_pos = tuple(settings.original_stencil_pos)
    brush.stencil_dimension = tuple(settings.original_stencil_dimension)


def _store_asset_reference(scene: Any, reference: BrushAssetReference | None) -> None:
    """임시 Brush의 원본 Asset 식별자를 Scene Property에 저장합니다."""
    settings = scene.projection_paint
    settings.original_brush_asset_library_type = (
        reference.library_type if reference is not None else ""
    )
    settings.original_brush_asset_library_identifier = (
        reference.library_identifier if reference is not None else ""
    )
    settings.original_brush_asset_relative_identifier = (
        reference.relative_identifier if reference is not None else ""
    )


def _stored_asset_reference(scene: Any) -> BrushAssetReference | None:
    """Scene Property에 완전한 원본 Asset 식별자가 있으면 반환합니다."""
    settings = scene.projection_paint
    if not settings.original_brush_asset_library_type:
        return None
    return BrushAssetReference(
        str(settings.original_brush_asset_library_type),
        str(settings.original_brush_asset_library_identifier),
        str(settings.original_brush_asset_relative_identifier),
    )


def _new_editable_brush(context: Any, scene: Any, source: Any):
    """연결 Asset Brush의 편집 가능한 임시 인스턴스를 활성화합니다."""
    reference = _brush_asset_reference(source)
    brush = source.copy()
    brush.name = f"{source.name} [Stencil]"
    tag_owned(
        brush,
        BRUSH_ROLE,
        dataset_uuid=str(scene.projection_paint.dataset_uuid),
        source_fingerprint=str(scene.projection_paint.source_fingerprint),
    )
    brush.use_fake_user = False
    window, area, region, _space, _rv3d = _viewport_parts(context)
    if window is None or area is None or region is None:
        raise StencilSessionError("임시 Brush를 활성화할 3D Viewport가 없습니다.")
    try:
        brush.asset_mark()
        with context.temp_override(
            window=window,
            screen=window.screen,
            area=area,
            region=region,
        ):
            result = bpy.ops.brush.asset_activate(
                asset_library_type="LOCAL",
                relative_asset_identifier=f"Brush/{brush.name}",
            )
        if result != {"FINISHED"} or scene.tool_settings.image_paint.brush is not brush:
            raise StencilSessionError("임시 Brush 활성화에 실패했습니다.")
        brush.asset_clear()
        brush.use_fake_user = False
        return brush, reference
    except Exception:
        if brush.asset_data is not None:
            brush.asset_clear()
        brush.use_fake_user = False
        if brush.users == 0:
            bpy.data.brushes.remove(brush)
        raise


def _remove_owned_brush(brush: Any, active_brush: Any) -> None:
    """비활성 애드온 소유 임시 Brush만 Blender 데이터에서 제거합니다."""
    if (
        brush is None
        or brush is active_brush
        or _pointer(brush) == 0
        or brush.get(OWNER_KEY) != OWNER_TOKEN
        or brush.get(RECORD_KIND_KEY) != BRUSH_ROLE
    ):
        return
    if brush.asset_data is not None:
        brush.asset_clear()
    brush.use_fake_user = False
    bpy.data.brushes.remove(brush)


def _migrate_legacy_brushes(context: Any, active_brush: Any):
    """활성 구버전 임시 Brush를 기본 Brush로 바꾸고 비활성 소유 Brush를 정리합니다."""
    legacy_active = bool(
        active_brush is not None
        and active_brush.get(OWNER_KEY) == OWNER_TOKEN
        and active_brush.get(RECORD_KIND_KEY) == BRUSH_ROLE
    )
    if legacy_active:
        window, area, region, _space, _rv3d = _viewport_parts(context)
        if window is None or area is None or region is None:
            raise StencilSessionError("구버전 Brush를 복구할 3D Viewport가 없습니다.")
        with context.temp_override(
            window=window,
            screen=window.screen,
            area=area,
            region=region,
        ):
            result = bpy.ops.brush.asset_activate(
                asset_library_type="ESSENTIALS",
                relative_asset_identifier=(
                    "brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard"
                ),
            )
        if result != {"FINISHED"}:
            raise StencilSessionError(f"기본 Paint Hard Brush 복구에 실패했습니다: {result}")
        active_brush = context.scene.tool_settings.image_paint.brush
    for brush in tuple(bpy.data.brushes):
        if (
            brush is active_brush
            or brush.get(OWNER_KEY) != OWNER_TOKEN
            or brush.get(RECORD_KIND_KEY) != BRUSH_ROLE
        ):
            continue
        _remove_owned_brush(brush, active_brush)
    return active_brush


def ensure_editable_brush(
    context: Any,
    scene: Any,
    *,
    brush_override: Any = None,
    migrate_legacy: bool = False,
):
    """현재 Brush를 직접 사용하거나 임시 편집 Brush 한 개로 전환합니다."""
    settings = scene.projection_paint
    source = (
        brush_override
        if brush_override is not None
        else scene.tool_settings.image_paint.brush
    )
    if migrate_legacy and brush_override is None:
        source = _migrate_legacy_brushes(context, source)
    if source is None or _pointer(source) == 0:
        raise StencilSessionError("활성 Texture Paint Brush가 없습니다.")
    previous = settings.stencil_brush
    if previous is not None and previous is not source:
        restore_original_brush(context, scene, remove_texture=False)
    _capture_brush_state(scene, source)
    if source.library is None and bool(source.is_editable):
        brush, reference, owns_brush = source, None, False
    else:
        brush, reference = _new_editable_brush(context, scene, source)
        _capture_brush_state(scene, brush)
        owns_brush = True
    brush.color = (1.0, 1.0, 1.0)
    settings.stencil_brush = brush
    settings.stencil_owns_brush = owns_brush
    settings.stencil_follow_active_brush = brush_override is None
    _store_asset_reference(scene, reference)
    return brush


def restore_original_brush(
    context: Any,
    scene: Any,
    *,
    remove_texture: bool = False,
    activate_original: bool = True,
) -> None:
    """현재 Brush 설정과 원본 Asset을 복구하고 임시 Brush를 정리합니다."""
    settings = scene.projection_paint
    brush = settings.stencil_brush
    _restore_brush_state(scene, brush)
    active_brush = scene.tool_settings.image_paint.brush
    if bool(settings.stencil_owns_brush):
        reference = _stored_asset_reference(scene)
        if activate_original and reference is not None:
            try:
                active_brush = _activate_brush_asset(context, reference)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                active_brush = scene.tool_settings.image_paint.brush
        _remove_owned_brush(brush, active_brush)
    settings.stencil_brush = None
    settings.original_brush_texture = None
    settings.stencil_original_state_valid = False
    settings.stencil_owns_brush = False
    _store_asset_reference(scene, None)
    if remove_texture:
        texture = settings.stencil_texture
        settings.stencil_texture = None
        if (
            _pointer(texture)
            and texture.users == 0
            and texture.get(OWNER_KEY) == OWNER_TOKEN
            and texture.get(RECORD_KIND_KEY) == TEXTURE_ROLE
        ):
            bpy.data.textures.remove(texture)


def start_stencil_session(
    context: Any,
    _paint_token: Any = None,
    *,
    dataset_uuid: str,
    source_fingerprint: str = "",
    brush_override: Any = None,
) -> StencilSession:
    """Blender Scene에 Brush와 단일 Stencil Texture 권위 Pointer를 준비합니다."""
    scene = context.scene
    validate_paint_contract(scene)
    settings = scene.projection_paint
    image_paint = scene.tool_settings.image_paint
    settings.stencil_original_use_unified_color = bool(
        image_paint.unified_paint_settings.use_unified_color
    )
    ensure_stencil_texture(
        scene,
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
    )
    ensure_editable_brush(
        context,
        scene,
        brush_override=brush_override,
        migrate_legacy=True,
    )
    image_paint.unified_paint_settings.use_unified_color = False
    settings.manual_stencil_disabled = False
    settings.stencil_outside_camera_view = False
    return _snapshot(context, scene)


def stage_stencil_source(
    session: StencilSession,
    *,
    image: Any,
    image_id: int,
    camera: Any = None,
    frame_corners: tuple[tuple[float, float], ...] | None = None,
) -> None:
    """현재 Frame Source와 Camera를 Blender Pointer 상태에 기록합니다."""
    session.validate()
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
        session.scene.camera = camera


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
    """현재 Scene Camera로 작업 Viewport의 Camera View를 확정합니다."""
    camera = session.scene.camera
    if camera is None or _pointer(camera) == 0:
        return
    if session.space is not None and _pointer(session.space):
        session.space.camera = camera
    if session.rv3d is not None and _pointer(session.rv3d):
        session.rv3d.view_perspective = "CAMERA"


def align_stencil_to_camera(context: Any, scene: Any) -> None:
    """현재 Camera Frame에 활성 Brush Stencil 위치와 크기를 맞춥니다."""
    session = _snapshot(context, scene)
    session.validate()
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
    texture.image = image
    brush.texture_slot.map_mode = "STENCIL"
    brush.stencil_pos = position
    brush.stencil_dimension = dimensions
    brush.texture = texture
    if brush.texture is not texture or brush.texture_slot.map_mode != "STENCIL":
        raise StencilSessionError("관리 Stencil Texture 연결을 확정하지 못했습니다.")


def _switch_active_brush(context: Any, scene: Any, active: Any) -> None:
    """이전 Brush를 복구하고 새 활성 Brush에 같은 관리 Texture를 준비합니다."""
    restore_original_brush(
        context,
        scene,
        remove_texture=False,
        activate_original=False,
    )
    ensure_editable_brush(context, scene, brush_override=active)
    scene.projection_paint.stencil_follow_active_brush = True


def disconnect_stencil(scene: Any) -> bool:
    """Camera View 이탈 시 Source를 유지하고 Brush Texture 연결만 해제합니다."""
    settings = scene.projection_paint
    brush = settings.stencil_brush
    texture = resolve_stencil_texture(scene)
    if brush is None or texture is None:
        return False
    if brush.texture is texture:
        brush.texture = None
    settings.stencil_outside_camera_view = True
    settings.paint_state = "FROZEN"
    return True


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
    """Python Session 없이 현재 Blender Source를 STENCIL Mapping으로 복구합니다."""
    current = session if session is not None else _snapshot(context)
    scene = current.scene
    settings = scene.projection_paint
    validate_paint_contract(scene)
    if settings.stencil_source_image is None or int(settings.active_image_id) <= 0:
        raise StencilSessionError("현재 Frame Source가 준비되지 않았습니다.")
    active = scene.tool_settings.image_paint.brush
    brush = settings.stencil_brush
    if bool(settings.stencil_follow_active_brush) and active is not brush:
        _switch_active_brush(context, scene, active)
    ensure_stencil_texture(
        scene,
        dataset_uuid=str(settings.dataset_uuid),
        source_fingerprint=str(settings.source_fingerprint),
    )
    try:
        current = _snapshot(context, scene)
        _enter_camera_view(current)
        align_stencil_to_camera(context, scene)
        settings.manual_stencil_disabled = False
        settings.stencil_outside_camera_view = False
        settings.paint_state = "ACTIVE"
        return int(settings.active_image_id)
    except Exception:
        brush = settings.stencil_brush
        if brush is not None and _pointer(brush):
            brush.texture = None
        settings.manual_stencil_disabled = True
        raise


def refresh_stencil(
    context: Any,
    session: StencilSession | None = None,
    *,
    refresh_viewport: bool = True,
) -> bool:
    """수동 해제를 존중하며 현재 Blender Source의 Stencil 정렬만 갱신합니다."""
    current = session if session is not None else _snapshot(context or bpy.context)
    scene = current.scene
    settings = scene.projection_paint
    active = scene.tool_settings.image_paint.brush
    brush = settings.stencil_brush
    switched_brush = False
    if bool(settings.stencil_follow_active_brush) and active is not brush:
        _switch_active_brush(context or bpy.context, scene, active)
        brush = settings.stencil_brush
        switched_brush = True
    texture = resolve_stencil_texture(scene)
    if (
        not switched_brush
        and not bool(settings.stencil_outside_camera_view)
        and (
            brush is None
            or brush.texture is not texture
            or brush.texture_slot.map_mode != "STENCIL"
        )
    ):
        settings.manual_stencil_disabled = True
        settings.paint_state = "MANUAL_DISABLED"
        return False
    if bool(settings.manual_stencil_disabled) or settings.stencil_source_image is None:
        return False
    if refresh_viewport:
        current = _snapshot(context or bpy.context, scene)
    align_stencil_to_camera(context or bpy.context, scene)
    settings.stencil_outside_camera_view = False
    settings.paint_state = "ACTIVE"
    return True


def stencil_view_token(session: StencilSession) -> tuple[object, ...]:
    """저비용 비교용 Camera View·Stencil 연결 상태 Token을 반환합니다."""
    current = _snapshot(bpy.context, session.scene)
    current.validate()
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
        str(rv3d.view_perspective) == "CAMERA",
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
        str(brush.texture_slot.map_mode),
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
    """사용자 Brush를 복구하고 Scene의 임시 Stencil Pointer를 정리합니다."""
    active_context = context if context is not None else bpy.context
    active_scene = scene if scene is not None else getattr(active_context, "scene", None)
    if active_scene is None:
        return
    settings = getattr(active_scene, "projection_paint", None)
    if settings is None:
        return
    texture = settings.stencil_texture
    if texture is not None and _pointer(texture):
        texture.image = None
    restore_original_brush(active_context, active_scene, remove_texture=True)
    unified = active_scene.tool_settings.image_paint.unified_paint_settings
    if not unified.use_unified_color:
        unified.use_unified_color = bool(settings.stencil_original_use_unified_color)
    settings.stencil_source_image = None
    settings.active_image_id = 0
    settings.current_image_id = ""
    settings.current_image_name = ""
    settings.stencil_frame_corners_json = ""
    settings.manual_stencil_disabled = False
    settings.stencil_outside_camera_view = False


def project_current_stencil(context: Any) -> int:
    """현재 Frame Source를 활성 카메라에서 보이는 Target 표면 전체에 투영합니다."""
    scene = context.scene
    settings = scene.projection_paint
    if not bool(settings.paint_enabled):
        raise StencilSessionError("먼저 Painting Mode를 활성화하십시오.")
    if bool(settings.paint_mask_mode):
        raise StencilSessionError("Layer를 선택한 뒤 Stencil을 투영하십시오.")
    from .paint_runtime import reconnect_current_frame

    image_id = reconnect_current_frame(context)
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

    def execute(self, context):
        """KDB-tree로 Camera를 선택하고 해당 Frame의 STENCIL Mapping을 복구합니다."""
        try:
            from .paint_runtime import reconnect_nearest_camera

            candidate = reconnect_nearest_camera(context)
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
        """Painting Mode와 유효한 Target·Source가 있을 때만 버튼을 활성화합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and settings.paint_enabled
            and settings.paint_target is not None
            and settings.stencil_source_image is not None
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
    "StencilSession",
    "StencilSessionError",
    "active_stencil_session",
    "align_stencil_to_camera",
    "bind_stencil_source",
    "camera_frame_stencil",
    "detach_source_image",
    "disconnect_stencil",
    "disconnect_stencil_for_camera_exit",
    "ensure_editable_brush",
    "ensure_stencil_texture",
    "finish_stencil_session",
    "project_camera_frame",
    "project_current_stencil",
    "reconnect_stencil_texture",
    "refresh_stencil",
    "restore_original_brush",
    "stage_stencil_source",
    "start_stencil_session",
    "stencil_view_token",
]
