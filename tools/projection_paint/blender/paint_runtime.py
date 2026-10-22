"""재생성 가능한 Frame Source Cache와 Camera View 추적만 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from .camera_scene import (
    CameraManifestEntry,
    activate_managed_frame,
    apply_projection_camera_state,
    consume_committed_frame_request,
    install_camera_runtime,
    projection_camera_session_is_active,
    runtime_for_scene,
    scene_manifest,
)
from .camera_spatial import (
    CameraCandidate,
    CameraKDBIndex,
    ViewportNavigationState,
    build_camera_kdb_index,
    camera_kdb_index_is_current,
    capture_viewport_navigation,
    restore_viewport_navigation,
    select_best_camera,
    viewport_pose,
)
from .frame_source import FrameSourceCache
from .ownership import (
    DATASET_REVISION_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    PROJECT_STENCIL_DRAW_BRUSH_ROLE,
    RECORD_KIND_KEY,
    ensure_scene_uuid,
    is_owned,
)
from .stencil_session import (
    StencilSessionError,
    active_draw_brush_for_reconnect,
    active_stencil_session,
    align_stencil_to_camera,
    bind_active_draw_brush,
    bind_stencil_source,
    detach_source_image,
    disconnect_stencil,
    reconnect_stencil_texture,
    resolve_stencil_connection,
    refresh_stencil,
    stencil_connection_allowed,
    stencil_view_token,
)


VIEWPORT_TRACK_INTERVAL = 0.1
OUTSIDE_CAMERA_VIEW_INTERVAL = 0.5
TOOL_EDGE_INTERVAL = 0.25
MAX_VIEWPORT_TRACK_RETRIES = 5


class PaintRuntimeError(RuntimeError):
    """Frame Source Cache와 Stencil 자동 연결 오류를 나타냅니다."""


@dataclass(slots=True)
class PaintRuntime:
    """Scene별 Frame Index와 Source LRU만 보관하는 폐기 가능 Cache입니다."""

    scene: Any
    scene_pointer: int
    scene_uuid: str
    entries: dict[int, CameraManifestEntry]
    image_paths: dict[int, Path]
    cache: FrameSourceCache
    camera_index: CameraKDBIndex | None = None
    viewport_token: tuple[object, ...] | None = None
    viewport_retry_count: int = 0
    tool_bridge: Any | None = None
    view_subscription_owner: Any | None = None
    projection_workspace_active: bool = False

    @property
    def stencil(self):
        """호환 진단용 현재 Blender Stencil Snapshot을 반환합니다."""
        return active_stencil_session(self.scene)


_RUNTIMES: dict[str, PaintRuntime] = {}


@dataclass(frozen=True, slots=True)
class _FrameSourceCommitSnapshot:
    """Frame Source Commit 실패 시 기존 Source metadata와 Render 상태를 복구합니다."""

    scene_camera: Any
    stencil_texture: Any
    stencil_texture_image: Any
    stencil_source_image: Any
    active_image_id: int
    current_image_id: str
    current_image_name: str
    frame_corners_json: str
    render_resolution: tuple[int, int]
    render_resolution_overridden: bool
    original_render_resolution: tuple[int, int]
    applied_render_resolution: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _ReconnectBrushSnapshot:
    """한 Reconnect 호출이 바꿀 현재 Draw Brush 필드만 원자 복구용으로 보관합니다."""

    brush: Any
    texture: Any
    map_mode: str
    angle: float
    color: tuple[float, float, float]
    stencil_pos: tuple[float, float]
    stencil_dimension: tuple[float, float]


@dataclass(frozen=True, slots=True)
class _PaintTargetSnapshot:
    """Reconnect와 무관한 Paint Target·Canvas·활성 Object·Material 상태를 보관합니다."""

    canvas: Any
    material: Any
    paint_active_slot: int | None
    active_object: Any
    active_material: Any
    active_material_index: int | None


@dataclass(frozen=True, slots=True)
class _ReconnectSnapshot:
    """최근접 Camera·Source·Brush 연결 호출의 원자 Rollback 상태를 보관합니다."""

    frame: int
    scene_camera: Any
    navigation: ViewportNavigationState
    tool_id: str
    active_brush: Any
    active_brush_choice: Any
    brush: _ReconnectBrushSnapshot
    bound_brush_state: _ReconnectBrushSnapshot | None
    paint_target: _PaintTargetSnapshot
    bound_brush: Any
    manual_stencil_disabled: bool
    stencil_outside_camera_view: bool
    paint_state: str
    stencil_texture: Any
    stencil_texture_image: Any
    stencil_source_image: Any
    active_image_id: int
    current_image_id: str
    current_image_name: str
    frame_corners_json: str
    render_resolution: tuple[int, int]
    render_resolution_overridden: bool
    original_render_resolution: tuple[int, int]
    applied_render_resolution: tuple[int, int]


def _pointer(value: Any) -> int:
    """삭제된 Blender RNA 객체를 안전하게 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def _runtime_key(scene: Any) -> str:
    """Scene의 저장 가능한 UUID를 Runtime Registry Key로 반환합니다."""
    return ensure_scene_uuid(scene)


def _image_paths(scene: Any) -> dict[int, Path]:
    """Scene의 영구 Image Path Manifest를 IMAGE_ID Index로 읽습니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        raise PaintRuntimeError("Projection Paint Scene 설정이 없습니다.")
    try:
        raw = json.loads(str(settings.image_path_manifest_json))
        paths = {int(image_id): Path(str(path)) for image_id, path in raw.items()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PaintRuntimeError("Source Image 경로 Manifest가 손상되었습니다.") from exc
    if not paths:
        raise PaintRuntimeError("Import된 Source Image 경로가 없습니다.")
    return paths


def active_paint_runtime(scene: Any | None = None) -> PaintRuntime | None:
    """현재 Scene의 유효한 폐기 가능 Paint Runtime을 반환합니다."""
    import bpy

    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    if active_scene is None:
        return None
    key = str(active_scene.get("pp_scene_uuid", "") or "")
    runtime = _RUNTIMES.get(key)
    if (
        runtime is None
        or runtime.scene is not active_scene
        or runtime.scene_pointer != _pointer(active_scene)
    ):
        if key:
            _RUNTIMES.pop(key, None)
        return None
    return runtime


def _frame_source_is_current(runtime: PaintRuntime) -> bool:
    """현재 Frame IMAGE_ID와 Blender Stencil Source가 같은지 확인합니다."""
    entry = runtime.entries.get(int(runtime.scene.frame_current))
    settings = getattr(runtime.scene, "projection_paint", None)
    if settings is not None:
        current_id = int(settings.active_image_id)
    else:
        current_id = int(getattr(getattr(runtime, "stencil", None), "source_image_id", 0) or 0)
    return bool(entry is not None and current_id == int(entry.image_id))


def _apply_camera_resolution(runtime: PaintRuntime, camera: Any, image: Any) -> None:
    """Source 해상도를 검증하고 Camera Frame의 Render 종횡비에 적용합니다."""

    from .camera_review import camera_dimensions

    width, height = (int(value) for value in image.size)
    expected = camera_dimensions(camera.data)
    if min(width, height) <= 0 or expected != (width, height):
        raise PaintRuntimeError(
            f"Camera 해상도 {expected}와 Source Image 해상도 {(width, height)}가 일치하지 않습니다."
        )
    settings = runtime.scene.projection_paint
    render = runtime.scene.render
    if not bool(settings.render_resolution_overridden):
        settings.original_render_width = int(render.resolution_x)
        settings.original_render_height = int(render.resolution_y)
        settings.render_resolution_overridden = True
    apply_projection_camera_state(
        runtime.scene,
        camera,
        render_resolution=(width, height, int(render.resolution_percentage)),
    )
    settings.applied_render_width = width
    settings.applied_render_height = height


def _camera_for_entry(runtime: PaintRuntime, entry: CameraManifestEntry):
    """Camera Runtime에서 Manifest 항목에 해당하는 Camera Object를 반환합니다."""
    camera_runtime = runtime_for_scene(runtime.scene)
    camera = (
        camera_runtime.objects.get(entry.camera_uuid)
        if camera_runtime is not None
        else None
    )
    if camera is None or _pointer(camera) == 0:
        raise PaintRuntimeError(f"IMAGE_ID {entry.image_id}의 Camera Object가 없습니다.")
    return camera


def _capture_frame_source_commit_snapshot(
    runtime: PaintRuntime,
) -> _FrameSourceCommitSnapshot:
    """Source Pointer를 쓰기 직전의 복구 상태를 불변 Snapshot으로 고정합니다."""

    scene = runtime.scene
    settings = scene.projection_paint
    texture = settings.stencil_texture
    return _FrameSourceCommitSnapshot(
        scene_camera=scene.camera,
        stencil_texture=texture,
        stencil_texture_image=(getattr(texture, "image", None) if _pointer(texture) else None),
        stencil_source_image=settings.stencil_source_image,
        active_image_id=int(settings.active_image_id),
        current_image_id=str(settings.current_image_id or ""),
        current_image_name=str(settings.current_image_name or ""),
        frame_corners_json=str(settings.stencil_frame_corners_json or ""),
        render_resolution=(int(scene.render.resolution_x), int(scene.render.resolution_y)),
        render_resolution_overridden=bool(settings.render_resolution_overridden),
        original_render_resolution=(
            int(settings.original_render_width),
            int(settings.original_render_height),
        ),
        applied_render_resolution=(
            int(settings.applied_render_width),
            int(settings.applied_render_height),
        ),
    )


def _restore_frame_source_commit_snapshot(
    runtime: PaintRuntime,
    snapshot: _FrameSourceCommitSnapshot,
) -> None:
    """실패한 Source Commit의 Pointer·metadata·Render 변경을 전 상태로 돌립니다."""

    scene = runtime.scene
    settings = scene.projection_paint
    apply_projection_camera_state(
        scene,
        snapshot.scene_camera,
        render_resolution=(
            snapshot.render_resolution[0],
            snapshot.render_resolution[1],
            int(scene.render.resolution_percentage),
        ),
    )
    settings.stencil_texture = snapshot.stencil_texture
    if _pointer(snapshot.stencil_texture):
        snapshot.stencil_texture.image = snapshot.stencil_texture_image
    settings.stencil_source_image = snapshot.stencil_source_image
    settings.active_image_id = snapshot.active_image_id
    settings.current_image_id = snapshot.current_image_id
    settings.current_image_name = snapshot.current_image_name
    settings.stencil_frame_corners_json = snapshot.frame_corners_json
    settings.render_resolution_overridden = snapshot.render_resolution_overridden
    settings.original_render_width, settings.original_render_height = (
        snapshot.original_render_resolution
    )
    settings.applied_render_width, settings.applied_render_height = (
        snapshot.applied_render_resolution
    )
    runtime.cache.pin(snapshot.active_image_id)


def _validate_frame_source_commit(
    runtime: PaintRuntime,
    entry: CameraManifestEntry,
    image: Any,
    camera: Any,
) -> None:
    """Commit된 Source Pointer·IMAGE_ID·Camera·Render 해상도 일치를 검증합니다."""

    scene = runtime.scene
    settings = scene.projection_paint
    texture = settings.stencil_texture
    if int(scene.frame_current) != int(entry.frame):
        raise PaintRuntimeError("Source Commit Frame이 최신 요청과 다릅니다.")
    if scene.camera is not camera:
        raise PaintRuntimeError("Source Commit Camera Pointer가 변경되었습니다.")
    if settings.stencil_source_image is not image or _pointer(image) == 0:
        raise PaintRuntimeError("Source Commit Image Pointer가 일치하지 않습니다.")
    if texture is None or not _pointer(texture) or texture.image is not image:
        raise PaintRuntimeError("Source Commit Texture Pointer가 일치하지 않습니다.")
    if int(settings.active_image_id) != int(entry.image_id):
        raise PaintRuntimeError("Source Commit IMAGE_ID가 일치하지 않습니다.")
    if str(settings.current_image_id or "") != str(entry.image_id):
        raise PaintRuntimeError("Source Commit IMAGE_ID metadata가 일치하지 않습니다.")
    if str(settings.current_image_name or "") != str(entry.image_name):
        raise PaintRuntimeError("Source Commit Image 이름 metadata가 일치하지 않습니다.")
    if (int(scene.render.resolution_x), int(scene.render.resolution_y)) != tuple(
        int(value) for value in image.size
    ):
        raise PaintRuntimeError("Source Commit Render 해상도가 Image와 다릅니다.")


def _stage_current_source(
    runtime: PaintRuntime,
    context: Any = None,
    *,
    expected_frame: int | None = None,
    expected_image_id: int | None = None,
    request_is_current: Callable[[], bool] | None = None,
    commit_validator: Callable[[], None] | None = None,
) -> int:
    """최신 Frame Token을 재검증하며 Image와 metadata를 원자적으로 Commit합니다."""
    import bpy

    frame = int(runtime.scene.frame_current)
    if expected_frame is not None and frame != int(expected_frame):
        raise PaintRuntimeError("예약한 Frame Source 요청이 변경되었습니다.")
    entry = runtime.entries.get(frame)
    if entry is None:
        raise PaintRuntimeError(f"현재 Frame {frame}에 관리 Camera가 없습니다.")
    if expected_image_id is not None and int(entry.image_id) != int(expected_image_id):
        raise PaintRuntimeError("예약한 IMAGE_ID Source 요청이 변경되었습니다.")
    camera_runtime = runtime_for_scene(runtime.scene)
    camera_token = (
        camera_runtime.camera_session_token if camera_runtime is not None else None
    )
    supplied_request = request_is_current or (lambda: True)

    def current_request() -> bool:
        """호출자 요청과 Projection Camera 세대가 모두 최신인지 확인합니다."""

        return bool(
            supplied_request()
            and camera_token is not None
            and projection_camera_session_is_active(
                runtime.scene,
                token=camera_token,
            )
        )

    if not current_request():
        raise PaintRuntimeError("최신 Frame Source 요청이 변경되었습니다.")
    path = runtime.image_paths.get(entry.image_id)
    if path is None:
        raise PaintRuntimeError(f"IMAGE_ID {entry.image_id}의 Source Image 경로가 없습니다.")
    camera = _camera_for_entry(runtime, entry)
    token = runtime.cache.begin_request(frame=frame, image_id=entry.image_id)
    image = runtime.cache.get(entry.image_id)
    if image is None:
        settings = runtime.scene.projection_paint
        existing = settings.stencil_source_image
        if (
            int(settings.review_background_image_id) == int(entry.image_id)
            and settings.review_background_image is not None
            and _pointer(settings.review_background_image)
        ):
            existing = settings.review_background_image
        if (
            (
                int(settings.active_image_id) == int(entry.image_id)
                or int(settings.review_background_image_id) == int(entry.image_id)
            )
            and existing is not None
            and _pointer(existing)
        ):
            image = runtime.cache.add_borrowed(entry.image_id, existing)
        else:
            image = runtime.cache.load_owned(token, path)
    active_context = context if context is not None else bpy.context
    snapshot = _capture_frame_source_commit_snapshot(runtime)

    def apply_source(current_image: Any, image_id: int) -> None:
        """검증된 최신 Image를 Blender 권위 Stencil Pointer에 기록합니다."""
        if not current_request():
            raise PaintRuntimeError("최신 Frame Source 요청이 변경되었습니다.")
        _apply_camera_resolution(runtime, camera, current_image)
        bind_stencil_source(
            active_context,
            runtime.scene,
            current_image,
            image_id,
            camera,
        )
        settings = runtime.scene.projection_paint
        settings.current_image_id = str(entry.image_id)
        settings.current_image_name = str(entry.image_name)
        if not current_request():
            raise PaintRuntimeError("최신 Frame Source 요청이 변경되었습니다.")
        _validate_frame_source_commit(runtime, entry, current_image, camera)
        if commit_validator is not None:
            commit_validator()
        if not current_request():
            raise PaintRuntimeError("최신 Frame Source 요청이 변경되었습니다.")

    try:
        committed = runtime.cache.commit(
            token,
            image,
            current_frame=int(runtime.scene.frame_current),
            apply=apply_source,
        )
        if not committed or not current_request():
            raise PaintRuntimeError(
                "최신 Frame Source 요청이 변경되어 연결을 취소했습니다."
            )
        _validate_frame_source_commit(runtime, entry, image, camera)
    except Exception:
        _restore_frame_source_commit_snapshot(runtime, snapshot)
        raise
    return entry.image_id


def _new_runtime(scene: Any) -> PaintRuntime:
    """Scene Manifest에서 폐기 가능한 Frame Index와 LRU를 새로 만듭니다."""
    from .lifecycle import lifecycle_registry

    manifest = scene_manifest(scene)
    if manifest is None or not manifest.entries:
        raise PaintRuntimeError("먼저 COLMAP Camera를 Import하십시오.")
    paths = _image_paths(scene)
    expected_ids = {entry.image_id for entry in manifest.entries}
    if set(paths) != expected_ids:
        raise PaintRuntimeError("Camera와 Source Image 경로의 IMAGE_ID가 일치하지 않습니다.")
    _prune_orphan_sources(scene, manifest.dataset_revision)
    key = _runtime_key(scene)
    runtime = PaintRuntime(
        scene=scene,
        scene_pointer=_pointer(scene),
        scene_uuid=key,
        entries={entry.frame: entry for entry in manifest.entries},
        image_paths=paths,
        cache=FrameSourceCache(
            scene=scene,
            session_epoch=int(lifecycle_registry().epoch),
            dataset_revision=manifest.dataset_revision,
            max_images=3,
            detach_callback=detach_source_image,
        ),
    )
    settings = scene.projection_paint
    if (
        int(settings.active_image_id) > 0
        and settings.stencil_source_image is not None
        and _pointer(settings.stencil_source_image)
    ):
        runtime.cache.add_borrowed(
            int(settings.active_image_id),
            settings.stencil_source_image,
        )
        runtime.cache.pin(int(settings.active_image_id))
    _RUNTIMES[key] = runtime
    return runtime


def _prune_orphan_sources(scene: Any, dataset_revision: int) -> None:
    """Runtime 재생성 때 참조 없는 동일 Dataset Source Image만 정리합니다."""
    import bpy

    current = scene.projection_paint.stencil_source_image
    for image in tuple(bpy.data.images):
        try:
            owned_orphan = bool(
                image is not current
                and image.users == 0
                and image.get(OWNER_KEY) == OWNER_TOKEN
                and image.get(RECORD_KIND_KEY) == "SOURCE_OWNED"
                and int(image.get(DATASET_REVISION_KEY, -1)) == int(dataset_revision)
            )
            if not owned_orphan:
                continue
            image.gl_free()
            image.buffers_free()
            bpy.data.images.remove(image)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue


def ensure_paint_runtime(scene: Any) -> PaintRuntime:
    """Runtime이 없어도 Blender 상태에서 즉시 재생성해 반환합니다."""
    return active_paint_runtime(scene) or _new_runtime(scene)


def _camera_runtime_for_paint(runtime: PaintRuntime):
    """Paint Runtime과 같은 Dataset의 유효한 Camera Runtime을 반환합니다."""

    camera_runtime = runtime_for_scene(runtime.scene)
    if camera_runtime is not None:
        return camera_runtime
    manifest = scene_manifest(runtime.scene)
    if manifest is None:
        raise PaintRuntimeError("현재 Scene에 Camera Manifest가 없습니다.")
    return install_camera_runtime(runtime.scene, manifest)


def _camera_index_for_paint(runtime: PaintRuntime) -> CameraKDBIndex:
    """현재 Camera Runtime과 일치하는 KDB Index를 재사용하거나 다시 구축합니다."""

    camera_runtime = _camera_runtime_for_paint(runtime)
    index = runtime.camera_index
    if index is None or not camera_kdb_index_is_current(index, camera_runtime):
        index = build_camera_kdb_index(camera_runtime)
        runtime.camera_index = index
    return index


def _capture_reconnect_brush(brush: Any) -> _ReconnectBrushSnapshot:
    """현재 Draw Brush에서 Reconnect가 직접 바꿀 필드만 캡처합니다."""

    if _pointer(brush) == 0:
        raise PaintRuntimeError("Reconnect 대상 Draw Brush가 삭제되었습니다.")
    return _ReconnectBrushSnapshot(
        brush=brush,
        texture=brush.texture,
        map_mode=str(brush.texture_slot.map_mode),
        angle=float(brush.texture_slot.angle),
        color=tuple(float(value) for value in brush.color),
        stencil_pos=tuple(float(value) for value in brush.stencil_pos),
        stencil_dimension=tuple(float(value) for value in brush.stencil_dimension),
    )


def _capture_paint_target_snapshot(context: Any) -> _PaintTargetSnapshot:
    """Paint Target의 Canvas·활성 Slot·Object·Material 상태를 캡처합니다."""

    scene = context.scene
    settings = scene.projection_paint
    image_paint = scene.tool_settings.image_paint
    material = getattr(settings, "paint_material", None)
    active_object = getattr(getattr(context, "view_layer", None), "objects", None)
    active_object = getattr(active_object, "active", None)
    return _PaintTargetSnapshot(
        canvas=getattr(image_paint, "canvas", None),
        material=material,
        paint_active_slot=(
            int(material.paint_active_slot) if _pointer(material) else None
        ),
        active_object=active_object,
        active_material=(
            getattr(active_object, "active_material", None)
            if _pointer(active_object)
            else None
        ),
        active_material_index=(
            int(active_object.active_material_index)
            if _pointer(active_object)
            else None
        ),
    )


def _capture_reconnect_snapshot(context: Any) -> _ReconnectSnapshot:
    """최근접 Camera 적용 전 직접 변경 가능한 상태만 원자 복구용으로 캡처합니다."""

    scene = context.scene
    settings = scene.projection_paint
    active_brush = scene.tool_settings.image_paint.brush
    from .paint_tool_bridge import active_brush_choice, active_tool_id

    texture = settings.stencil_texture
    bound_brush = settings.stencil_brush
    return _ReconnectSnapshot(
        frame=int(scene.frame_current),
        scene_camera=scene.camera,
        navigation=capture_viewport_navigation(context),
        tool_id=active_tool_id(context),
        active_brush=active_brush,
        active_brush_choice=active_brush_choice(scene),
        brush=_capture_reconnect_brush(active_brush),
        bound_brush_state=(
            _capture_reconnect_brush(bound_brush)
            if _pointer(bound_brush) and bound_brush is not active_brush
            else None
        ),
        paint_target=_capture_paint_target_snapshot(context),
        bound_brush=bound_brush,
        manual_stencil_disabled=bool(settings.manual_stencil_disabled),
        stencil_outside_camera_view=bool(settings.stencil_outside_camera_view),
        paint_state=str(settings.paint_state),
        stencil_texture=texture,
        stencil_texture_image=(
            getattr(texture, "image", None) if _pointer(texture) else None
        ),
        stencil_source_image=settings.stencil_source_image,
        active_image_id=int(settings.active_image_id),
        current_image_id=str(settings.current_image_id or ""),
        current_image_name=str(settings.current_image_name or ""),
        frame_corners_json=str(settings.stencil_frame_corners_json or ""),
        render_resolution=(int(scene.render.resolution_x), int(scene.render.resolution_y)),
        render_resolution_overridden=bool(settings.render_resolution_overridden),
        original_render_resolution=(
            int(settings.original_render_width),
            int(settings.original_render_height),
        ),
        applied_render_resolution=(
            int(settings.applied_render_width),
            int(settings.applied_render_height),
        ),
    )


def _restore_reconnect_brush(snapshot: _ReconnectBrushSnapshot) -> None:
    """Reconnect가 실제로 바꾼 Brush 필드만 원래 값으로 되돌립니다."""

    brush = snapshot.brush
    if _pointer(brush) == 0:
        raise PaintRuntimeError("Rollback 대상 Draw Brush가 삭제되었습니다.")
    if brush.texture is not snapshot.texture:
        brush.texture = snapshot.texture
    if str(brush.texture_slot.map_mode) != snapshot.map_mode:
        brush.texture_slot.map_mode = snapshot.map_mode
    if float(brush.texture_slot.angle) != snapshot.angle:
        brush.texture_slot.angle = snapshot.angle
    if tuple(float(value) for value in brush.color) != snapshot.color:
        brush.color = snapshot.color
    if tuple(float(value) for value in brush.stencil_pos) != snapshot.stencil_pos:
        brush.stencil_pos = snapshot.stencil_pos
    if (
        tuple(float(value) for value in brush.stencil_dimension)
        != snapshot.stencil_dimension
    ):
        brush.stencil_dimension = snapshot.stencil_dimension


def _restore_reconnect_active_brush(
    context: Any,
    scene: Any,
    snapshot: _ReconnectSnapshot,
) -> None:
    """첫 Reconnect 실패 시 원래 Blender Draw Brush Asset을 다시 활성화합니다."""

    image_paint = scene.tool_settings.image_paint
    if image_paint.brush is snapshot.active_brush:
        return
    if snapshot.active_brush_choice is None:
        image_paint.brush = snapshot.active_brush
        return
    from .paint_tool_bridge import activate_brush_asset

    restored = activate_brush_asset(
        context,
        scene,
        snapshot.active_brush_choice,
        expected_tool_id=snapshot.tool_id,
        expected_brush_type="DRAW",
    )
    if restored is not snapshot.active_brush:
        raise PaintRuntimeError("Rollback 뒤 원래 Draw Brush Pointer가 일치하지 않습니다.")


def _restore_reconnect_snapshot(
    runtime: PaintRuntime,
    context: Any,
    snapshot: _ReconnectSnapshot,
) -> str:
    """실패한 Reconnect가 바꾼 Camera·Source·Brush·Paint Target만 되돌립니다."""

    scene = runtime.scene
    settings = scene.projection_paint
    errors: list[str] = []
    active_after_failure = scene.tool_settings.image_paint.brush
    bound_after_failure = settings.stencil_brush
    created_brush = next(
        (
            brush
            for brush in (bound_after_failure, active_after_failure)
            if (
                _pointer(brush)
                and brush is not snapshot.bound_brush
                and brush is not snapshot.active_brush
                and is_owned(brush, kind=PROJECT_STENCIL_DRAW_BRUSH_ROLE)
            )
        ),
        None,
    )
    runtime.cache.invalidate()
    try:
        scene.frame_set(snapshot.frame)
        apply_projection_camera_state(
            scene,
            snapshot.scene_camera,
            render_resolution=(
                snapshot.render_resolution[0],
                snapshot.render_resolution[1],
                int(scene.render.resolution_percentage),
            ),
        )
        restore_viewport_navigation(context, snapshot.navigation)
    except Exception as exc:
        errors.append(str(exc))
    try:
        _restore_reconnect_active_brush(context, scene, snapshot)
        _restore_reconnect_brush(snapshot.brush)
        if snapshot.bound_brush_state is not None:
            _restore_reconnect_brush(snapshot.bound_brush_state)
    except Exception as exc:
        errors.append(str(exc))
    try:
        settings.stencil_texture = snapshot.stencil_texture
        if _pointer(snapshot.stencil_texture):
            snapshot.stencil_texture.image = snapshot.stencil_texture_image
        settings.stencil_source_image = snapshot.stencil_source_image
        settings.active_image_id = snapshot.active_image_id
        settings.current_image_id = snapshot.current_image_id
        settings.current_image_name = snapshot.current_image_name
        settings.stencil_frame_corners_json = snapshot.frame_corners_json
        settings.stencil_brush = snapshot.bound_brush
    except Exception as exc:
        errors.append(str(exc))
    if _pointer(created_brush):
        try:
            import bpy

            if scene.tool_settings.image_paint.brush is created_brush:
                raise PaintRuntimeError("새 프로젝트 전용 Brush가 아직 활성 상태입니다.")
            bpy.data.brushes.remove(created_brush)
        except Exception as exc:
            errors.append(str(exc))
    try:
        paint_target = snapshot.paint_target
        image_paint = scene.tool_settings.image_paint
        image_paint.canvas = paint_target.canvas
        if _pointer(paint_target.material) and paint_target.paint_active_slot is not None:
            paint_target.material.paint_active_slot = paint_target.paint_active_slot
        view_layer = getattr(context, "view_layer", None)
        if view_layer is not None:
            view_layer.objects.active = paint_target.active_object
        active_object = paint_target.active_object
        if _pointer(active_object):
            if paint_target.active_material_index is not None:
                active_object.active_material_index = paint_target.active_material_index
            if getattr(active_object, "active_material", None) is not paint_target.active_material:
                active_object.active_material = paint_target.active_material
    except Exception as exc:
        errors.append(str(exc))
    try:
        settings.render_resolution_overridden = snapshot.render_resolution_overridden
        settings.original_render_width, settings.original_render_height = (
            snapshot.original_render_resolution
        )
        settings.applied_render_width, settings.applied_render_height = (
            snapshot.applied_render_resolution
        )
    except Exception as exc:
        errors.append(str(exc))
    settings.manual_stencil_disabled = snapshot.manual_stencil_disabled
    settings.stencil_outside_camera_view = snapshot.stencil_outside_camera_view
    settings.paint_state = snapshot.paint_state
    resolve_stencil_connection(scene, context)
    return "; ".join(error for error in errors if error)


def _validate_reconnect_invariants(
    context: Any,
    scene: Any,
    snapshot: _ReconnectSnapshot,
    expected_active_brush: Any,
) -> None:
    """Reconnect가 전용 Draw Brush 외 Tool·Paint Target을 바꾸지 않았는지 검증합니다."""

    from .paint_tool_bridge import active_tool_id

    if active_tool_id(context) != snapshot.tool_id:
        raise PaintRuntimeError("Reconnect가 Blender Workspace Tool을 변경했습니다.")
    active = scene.tool_settings.image_paint.brush
    if active is not expected_active_brush:
        raise PaintRuntimeError("Reconnect 결과 Draw Brush Pointer가 일치하지 않습니다.")
    paint_target = snapshot.paint_target
    if scene.tool_settings.image_paint.canvas is not paint_target.canvas:
        raise PaintRuntimeError("Reconnect가 Paint Canvas를 변경했습니다.")
    if _pointer(paint_target.material) and (
        int(paint_target.material.paint_active_slot) != paint_target.paint_active_slot
    ):
        raise PaintRuntimeError("Reconnect가 Material 활성 Paint Slot을 변경했습니다.")
    active_object = getattr(getattr(context, "view_layer", None), "objects", None)
    active_object = getattr(active_object, "active", None)
    if active_object is not paint_target.active_object:
        raise PaintRuntimeError("Reconnect가 활성 Paint Object를 변경했습니다.")
    if _pointer(active_object) and active_object.active_material is not paint_target.active_material:
        raise PaintRuntimeError("Reconnect가 활성 Paint Material을 변경했습니다.")


def _clear_view_subscription(runtime: PaintRuntime) -> None:
    """Runtime의 Camera View Message Bus 구독을 안전하게 해제합니다."""
    import bpy

    owner = runtime.view_subscription_owner
    runtime.view_subscription_owner = None
    if owner is not None:
        bpy.msgbus.clear_by_owner(owner)


def _camera_view_changed(scene_uuid: str) -> None:
    """Camera View 변경은 Blender가 저장한 현재 Draw Brush Stencil을 그대로 유지합니다."""

    del scene_uuid


def _paint_context_changed(scene_uuid: str) -> None:
    """Workspace 복귀 Edge에서 Tool 기준선과 Camera View 추적을 다시 시작합니다."""
    import bpy

    runtime = _RUNTIMES.get(str(scene_uuid))
    if runtime is None:
        return
    active = _projection_runtime_is_active(runtime)
    if not active:
        runtime.projection_workspace_active = False
        runtime.viewport_retry_count = 0
        return
    if runtime.projection_workspace_active:
        return
    _start_runtime_paint_tool_edge_tracking(runtime)
    _start_runtime_viewport_tracking(runtime)


def _subscribe_camera_view(runtime: PaintRuntime) -> None:
    """Workspace·Object Mode 복귀만 Tool 기준선 재설정을 위해 구독합니다."""
    import bpy

    _clear_view_subscription(runtime)
    owner = object()
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Window, "workspace"),
        owner=owner,
        args=(runtime.scene_uuid,),
        notify=_paint_context_changed,
    )
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Object, "mode"),
        owner=owner,
        args=(runtime.scene_uuid,),
        notify=_paint_context_changed,
    )
    runtime.view_subscription_owner = owner


def _projection_runtime_is_active(runtime: PaintRuntime) -> bool:
    """전용 작업공간의 실제 활성 Target이 Texture Paint인지 확인합니다."""
    import bpy

    settings = getattr(runtime.scene, "projection_paint", None)
    window = getattr(bpy.context, "window", None)
    workspace = getattr(settings, "paint_workspace", None) if settings is not None else None
    target = getattr(settings, "paint_target", None) if settings is not None else None
    if window is None or workspace is None or target is None:
        return False
    from .paint_workspace import WORKSPACE_KIND
    from .ownership import is_owned

    view_layer = getattr(bpy.context, "view_layer", None)
    return bool(
        window.workspace is workspace
        and is_owned(workspace, kind=WORKSPACE_KIND)
        and str(getattr(target, "mode", "")) == "TEXTURE_PAINT"
        and view_layer is not None
        and getattr(view_layer.objects, "active", None) is target
    )


def _sync_stencil_connection_for_current_view(runtime: PaintRuntime, context: Any) -> bool:
    """Camera View에서는 같은 Draw Brush만 정렬하고 이탈 시 연결만 해제합니다."""

    scene = runtime.scene
    settings = scene.projection_paint
    session = active_stencil_session(scene)
    if session is None or bool(settings.paint_mask_mode):
        return False
    token = stencil_view_token(session)
    if not bool(token[0]):
        disconnect_stencil(scene)
        return False
    status = resolve_stencil_connection(scene, context)
    same_brush = status.active_brush is status.bound_brush and status.bound_brush is not None
    can_restore = status.state == "CONNECTED" or (
        status.state == "CAMERA_SUSPENDED"
        and bool(settings.stencil_outside_camera_view)
    )
    if not same_brush or not can_restore:
        return False
    align_stencil_to_camera(context, scene)
    settings.stencil_outside_camera_view = False
    resolve_stencil_connection(scene, context)
    return True


def _track_runtime(runtime: PaintRuntime) -> float | None:
    """임시 지우개 연결을 보존하며 Camera View 이동·확대와 이탈을 추적합니다."""

    import bpy

    from .stencil_erase import reconcile_stencil_erase

    if reconcile_stencil_erase(runtime.scene):
        return VIEWPORT_TRACK_INTERVAL

    settings = runtime.scene.projection_paint
    if not _projection_runtime_is_active(runtime):
        runtime.projection_workspace_active = False
        runtime.viewport_token = None
        runtime.viewport_retry_count = 0
        window = getattr(bpy.context, "window", None)
        return (
            OUTSIDE_CAMERA_VIEW_INTERVAL
            if window is not None
            and getattr(window, "workspace", None) is getattr(settings, "paint_workspace", None)
            else None
        )
    runtime.projection_workspace_active = True
    if bool(settings.paint_mask_mode) or bool(settings.manual_stencil_disabled):
        return OUTSIDE_CAMERA_VIEW_INTERVAL
    session = active_stencil_session(runtime.scene)
    if session is None:
        return OUTSIDE_CAMERA_VIEW_INTERVAL
    try:
        token = stencil_view_token(session)
        if not bool(token[0]):
            disconnect_stencil(runtime.scene)
            runtime.viewport_token = stencil_view_token(session)
            runtime.viewport_retry_count = 0
            return OUTSIDE_CAMERA_VIEW_INTERVAL
        if not _frame_source_is_current(runtime):
            runtime.viewport_retry_count = 0
            return VIEWPORT_TRACK_INTERVAL
        if token == runtime.viewport_token:
            runtime.viewport_retry_count = 0
            return VIEWPORT_TRACK_INTERVAL
        runtime.viewport_token = token
        connected = _sync_stencil_connection_for_current_view(runtime, bpy.context)
        runtime.viewport_token = stencil_view_token(session)
        runtime.viewport_retry_count = 0
        return (
            VIEWPORT_TRACK_INTERVAL
            if connected or bool(runtime.viewport_token[0])
            else OUTSIDE_CAMERA_VIEW_INTERVAL
        )
    except StencilSessionError:
        runtime.viewport_retry_count += 1
        if runtime.viewport_retry_count <= MAX_VIEWPORT_TRACK_RETRIES:
            return VIEWPORT_TRACK_INTERVAL
        raise


def _viewport_tracking_tick() -> float | None:
    """등록된 Paint Runtime의 다음 View 추적 간격을 하나로 병합합니다."""
    delays = tuple(
        delay
        for runtime in tuple(_RUNTIMES.values())
        for delay in (_track_runtime(runtime),)
        if delay is not None
    )
    return min(delays) if delays else None


def _track_paint_tool_edge(runtime: PaintRuntime) -> float | None:
    """임시 지우개를 도구 변경으로 기록하지 않고 실제 도구 전환만 전달합니다."""

    import bpy

    from .stencil_erase import reconcile_stencil_erase

    if reconcile_stencil_erase(runtime.scene):
        return TOOL_EDGE_INTERVAL

    if not _projection_runtime_is_active(runtime):
        runtime.projection_workspace_active = False
        return None
    from .paint_tool_bridge import (
        detect_paint_tool_edge,
        initialize_paint_tool_bridge,
    )
    from .paint_workspace import initialize_native_paint_tool_regions

    window = bpy.context.window
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            initialize_native_paint_tool_regions(window.workspace, area)

    if runtime.tool_bridge is None:
        runtime.tool_bridge = initialize_paint_tool_bridge(bpy.context, runtime.scene)
        runtime.projection_workspace_active = True
        return TOOL_EDGE_INTERVAL
    try:
        detect_paint_tool_edge(
            bpy.context,
            runtime.scene,
            runtime.tool_bridge,
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        settings = runtime.scene.projection_paint
        settings.status = f"Blender Paint Tool 전환 연결 실패: {exc}"
        settings.status_kind = "ERROR"
    return TOOL_EDGE_INTERVAL


def _paint_tool_edge_tick() -> float | None:
    """등록된 Runtime 중 활성 작업공간의 Blender Paint 도구 전환만 감지합니다."""

    delays = tuple(
        delay
        for runtime in tuple(_RUNTIMES.values())
        for delay in (_track_paint_tool_edge(runtime),)
        if delay is not None
    )
    return min(delays) if delays else None


def _start_runtime_paint_tool_edge_tracking(runtime: PaintRuntime) -> None:
    """검증된 Runtime의 현재 Tool 기준선과 Tool Edge Timer를 함께 시작합니다."""

    import bpy
    from .paint_tool_bridge import initialize_paint_tool_bridge

    runtime.tool_bridge = initialize_paint_tool_bridge(
        bpy.context,
        runtime.scene,
        runtime.tool_bridge,
    )
    runtime.projection_workspace_active = True
    from .lifecycle import lifecycle_registry

    lifecycle_registry().register_timer(
        _paint_tool_edge_tick,
        component="paint_tool_edge_tracking",
        first_interval=0.0,
    )


def start_paint_tool_edge_tracking(scene: Any | None = None) -> None:
    """현재 Tool을 기준선으로 기록한 뒤 저빈도 Tool Edge Timer를 시작합니다."""

    import bpy

    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    if active_scene is None:
        return
    runtime = ensure_paint_runtime(active_scene)
    if not _projection_runtime_is_active(runtime):
        return
    _start_runtime_paint_tool_edge_tracking(runtime)


def _start_runtime_viewport_tracking(runtime: PaintRuntime) -> None:
    """검증된 Runtime에 Camera View Message Bus와 추적 Timer를 연결합니다."""

    runtime.projection_workspace_active = True
    _subscribe_camera_view(runtime)
    settings = runtime.scene.projection_paint
    if bool(settings.paint_mask_mode) or bool(settings.manual_stencil_disabled):
        return
    if active_stencil_session(runtime.scene) is None:
        return
    runtime.viewport_token = None
    from .lifecycle import lifecycle_registry

    lifecycle_registry().register_timer(
        _viewport_tracking_tick,
        component="stencil_view_tracking",
        first_interval=0.0,
    )


def start_viewport_tracking(scene: Any | None = None) -> None:
    """Camera View 이동·확대와 이탈을 감지하는 저비용 Timer를 시작합니다."""
    import bpy

    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    if active_scene is None:
        return
    runtime = ensure_paint_runtime(active_scene)
    if not _projection_runtime_is_active(runtime):
        return
    _start_runtime_viewport_tracking(runtime)


def start_paint_runtime(
    context: Any,
    paint_session: Any = None,
    *,
    reconnect_on_start: bool = True,
) -> PaintRuntime:
    """Scene별 Frame Cache와 Source metadata를 준비하고 Tool 기준선만 기록합니다."""
    scene = getattr(paint_session, "scene", None) or context.scene
    runtime = ensure_paint_runtime(scene)
    try:
        _stage_current_source(runtime, context)
        start_paint_tool_edge_tracking(scene)
        if reconnect_on_start:
            start_viewport_tracking(scene)
    except Exception:
        stop_paint_runtime(context, scene=scene, restore_resolution=False)
        raise
    return runtime


def refresh_frame_source(
    scene: Any,
    *,
    expected_frame: int | None = None,
    expected_image_id: int | None = None,
    request_is_current: Callable[[], bool] | None = None,
    commit_validator: Callable[[], None] | None = None,
) -> bool:
    """Frame Source를 최신 요청으로 원자 갱신하고 성공 여부를 반환합니다."""

    settings = getattr(scene, "projection_paint", None)
    if settings is None or not projection_camera_session_is_active(scene):
        return False
    try:
        runtime = ensure_paint_runtime(scene)
        _stage_current_source(
            runtime,
            expected_frame=expected_frame,
            expected_image_id=expected_image_id,
            request_is_current=request_is_current,
            commit_validator=commit_validator,
        )
        resolve_stencil_connection(scene)
        return True
    except Exception as exc:
        settings.status = f"Frame Source 갱신 실패: {exc}"
        settings.status_kind = "ERROR"
        settings.stencil_state = "ERROR"
        settings.stencil_reason = str(exc)
        return False


def reconnect_current_frame(context: Any) -> int:
    """현재 Frame Source만 준비하고 사용자의 Brush Stencil 설정은 변경하지 않습니다."""
    scene = context.scene
    if bool(scene.projection_paint.paint_mask_mode):
        raise PaintRuntimeError("Mask 레이어에서는 Stencil을 연결할 수 없습니다.")
    try:
        from .overlay_migration import repair_managed_overlay_if_needed

        repair_managed_overlay_if_needed(context)
    except RuntimeError as exc:
        raise PaintRuntimeError(f"관리 Overlay UDIM 복구 실패: {exc}") from exc
    runtime = ensure_paint_runtime(scene)
    if not _projection_runtime_is_active(runtime):
        raise PaintRuntimeError("전용 작업공간의 실제 Texture Paint Target이 아닙니다.")
    image_id = _stage_current_source(runtime, context)
    consume_committed_frame_request(
        scene,
        int(scene.frame_current),
        int(image_id),
    )
    return image_id


def resume_stencil_after_layer_selection(context: Any) -> bool:
    """일반 Layer 복귀 시 자동 연결 없이 Stencil 상태와 View 추적만 재개합니다."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    runtime = active_paint_runtime(scene)
    if runtime is None or not _projection_runtime_is_active(runtime):
        return False
    resolve_stencil_connection(scene, context)
    start_viewport_tracking(scene)
    return False


def reconnect_nearest_camera(
    context: Any,
    *,
    bind_active_brush: bool = False,
) -> CameraCandidate:
    """최근접 Camera Transaction에서 Operator 권한일 때만 활성 Draw Brush를 채택합니다."""

    scene = context.scene
    settings = scene.projection_paint
    if bool(settings.paint_mask_mode):
        raise PaintRuntimeError("Mask 레이어에서는 Stencil을 연결할 수 없습니다.")
    if bind_active_brush:
        active_draw_brush_for_reconnect(context, scene)
    runtime = ensure_paint_runtime(scene)
    if not _projection_runtime_is_active(runtime):
        raise PaintRuntimeError("전용 작업공간의 실제 Texture Paint Target이 아닙니다.")
    pose = viewport_pose(context)
    snapshot = _capture_reconnect_snapshot(context)
    candidate = select_best_camera(_camera_index_for_paint(runtime), pose)
    source_path = runtime.image_paths.get(int(candidate.entry.image_id))
    if source_path is None or not source_path.is_file():
        raise PaintRuntimeError(
            f"IMAGE_ID {candidate.entry.image_id}의 Source Image 파일이 없습니다."
        )
    mutated = False
    expected_active_brush = snapshot.active_brush
    try:
        mutated = True
        _camera_for_entry(runtime, candidate.entry)
        activate_managed_frame(
            scene,
            candidate.entry.frame,
            context,
            refresh_source=False,
        )
        image_id = _stage_current_source(runtime, context)
        if int(image_id) != int(candidate.entry.image_id):
            raise PaintRuntimeError("선택한 Camera와 연결된 IMAGE_ID가 일치하지 않습니다.")
        if bind_active_brush:
            expected_active_brush = bind_active_draw_brush(context, scene)
        reconnect_stencil_texture(context)
        _validate_reconnect_invariants(
            context,
            scene,
            snapshot,
            expected_active_brush,
        )
        start_viewport_tracking(scene)
        from .overlay_migration import repair_managed_overlay_if_needed

        repair_managed_overlay_if_needed(context)
        consume_committed_frame_request(
            scene,
            int(candidate.entry.frame),
            int(candidate.entry.image_id),
        )
    except Exception as exc:
        rollback_error = (
            _restore_reconnect_snapshot(runtime, context, snapshot) if mutated else ""
        )
        detail = f"; Rollback 실패: {rollback_error}" if rollback_error else ""
        raise PaintRuntimeError(f"최근접 Camera 재연결 실패: {exc}{detail}") from exc
    settings.status = (
        f"IMAGE_ID {candidate.entry.image_id} · Frame {candidate.entry.frame} · "
        f"거리 {candidate.distance:.3f} · 시선 일치 {candidate.alignment:.3f}"
    )
    settings.status_kind = "SUCCESS"
    return candidate


def pause_paint_runtimes(_reason: str = "", _epoch: int = 0) -> None:
    """Undo·Redo·Load 전에 Blender ID를 쓰지 않고 Callback Cache만 폐기합니다."""
    from .lifecycle import lifecycle_registry

    lifecycle_registry().unregister_timer(_viewport_tracking_tick)
    lifecycle_registry().unregister_timer(_paint_tool_edge_tick)
    for runtime in tuple(_RUNTIMES.values()):
        _clear_view_subscription(runtime)
        runtime.cache.invalidate(session_epoch=int(_epoch) if _epoch else None)
    _RUNTIMES.clear()


def schedule_paint_reconcile(scene: Any, reason: str) -> None:
    """수명주기 완료 뒤 Blender 상태 Reconcile과 Runtime 재생성을 한 번 예약합니다."""
    scene_pointer = _pointer(scene)

    def reconcile_after_lifecycle() -> None:
        """현재 Scene Pointer가 유효할 때 Paint 연결을 Blender 상태에서 복구합니다."""
        import bpy

        if (
            _pointer(scene) != scene_pointer
            or not projection_camera_session_is_active(scene)
        ):
            return None
        from .paint_state import reconcile_paint_state

        snapshot = reconcile_paint_state(bpy.context, reason)
        if snapshot.scene is scene and snapshot.state in {"ACTIVE", "FROZEN", "MANUAL_DISABLED"}:
            ensure_paint_runtime(scene)
            start_paint_tool_edge_tracking(scene)
            if bool(scene.projection_paint.paint_mask_mode):
                disconnect_stencil(scene)
            elif snapshot.state == "ACTIVE":
                refresh_frame_source(scene)
            elif snapshot.state == "FROZEN":
                start_viewport_tracking(scene)
        return None

    from .lifecycle import lifecycle_registry

    lifecycle_registry().register_timer(
        reconcile_after_lifecycle,
        component="paint_reconcile",
        first_interval=0.0,
    )


def stop_paint_runtime(
    context: Any = None,
    *,
    scene: Any = None,
    restore_resolution: bool = True,
) -> None:
    """임시 지우개와 장면 콜백·소스 캐시를 정리하고 필요 시 렌더 크기를 복구합니다."""
    import bpy

    active_scene = scene or getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    if active_scene is None:
        return
    from .stencil_erase import restore_stencil_erase

    restore_stencil_erase(active_scene)
    key = str(active_scene.get("pp_scene_uuid", "") or "")
    runtime = _RUNTIMES.pop(key, None)
    if runtime is not None:
        _clear_view_subscription(runtime)
        runtime.cache.close(preserve_pinned=True)
    if not _RUNTIMES:
        from .lifecycle import lifecycle_registry

        lifecycle_registry().unregister_timer(_viewport_tracking_tick)
        lifecycle_registry().unregister_timer(_paint_tool_edge_tick)
    settings = getattr(active_scene, "projection_paint", None)
    if (
        settings is None
        or not restore_resolution
        or not projection_camera_session_is_active(active_scene)
    ):
        return
    if bool(settings.render_resolution_overridden):
        current = (
            int(active_scene.render.resolution_x),
            int(active_scene.render.resolution_y),
        )
        applied = (
            int(settings.applied_render_width),
            int(settings.applied_render_height),
        )
        if (
            current == applied
            and settings.original_render_width > 0
            and settings.original_render_height > 0
        ):
            active_scene.render.resolution_x = int(settings.original_render_width)
            active_scene.render.resolution_y = int(settings.original_render_height)
        settings.render_resolution_overridden = False
        settings.applied_render_width = 0
        settings.applied_render_height = 0


def clear_paint_runtime_registry() -> None:
    """테스트와 Add-on 해제를 위해 모든 폐기 가능 Runtime을 안전하게 닫습니다."""
    for runtime in tuple(_RUNTIMES.values()):
        stop_paint_runtime(scene=runtime.scene, restore_resolution=False)
    _RUNTIMES.clear()


__all__ = [
    "PaintRuntime",
    "PaintRuntimeError",
    "active_paint_runtime",
    "clear_paint_runtime_registry",
    "ensure_paint_runtime",
    "pause_paint_runtimes",
    "reconnect_current_frame",
    "reconnect_nearest_camera",
    "refresh_frame_source",
    "resume_stencil_after_layer_selection",
    "schedule_paint_reconcile",
    "start_paint_runtime",
    "start_paint_tool_edge_tracking",
    "start_viewport_tracking",
    "stop_paint_runtime",
]
