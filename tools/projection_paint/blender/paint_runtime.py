"""재생성 가능한 Frame Source Cache와 Camera View 추적만 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .camera_scene import (
    CameraManifestEntry,
    activate_managed_frame,
    install_camera_runtime,
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
    RECORD_KIND_KEY,
    ensure_scene_uuid,
)
from .stencil_session import (
    StencilSessionError,
    active_stencil_session,
    bind_stencil_source,
    detach_source_image,
    disconnect_stencil,
    reconnect_stencil_texture,
    refresh_stencil,
    stencil_view_token,
)


VIEWPORT_TRACK_INTERVAL = 0.1
OUTSIDE_CAMERA_VIEW_INTERVAL = 0.5
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
    view_subscription_owner: Any | None = None
    projection_workspace_active: bool = False

    @property
    def stencil(self):
        """호환 진단용 현재 Blender Stencil Snapshot을 반환합니다."""
        return active_stencil_session(self.scene)


_RUNTIMES: dict[str, PaintRuntime] = {}


@dataclass(frozen=True, slots=True)
class _ReconnectSnapshot:
    """최근접 Camera 적용 실패 시 복구할 Scene과 Viewport 상태를 보관합니다."""

    frame: int
    scene_camera: Any
    navigation: ViewportNavigationState
    manual_stencil_disabled: bool
    stencil_outside_camera_view: bool
    paint_state: str


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
    width, height = (int(value) for value in image.size)
    expected = (
        int(camera.data.get("pp_camera_width", 0)),
        int(camera.data.get("pp_camera_height", 0)),
    )
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
    if (int(render.resolution_x), int(render.resolution_y)) != (width, height):
        render.resolution_x = width
        render.resolution_y = height
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


def _stage_current_source(runtime: PaintRuntime, context: Any = None) -> int:
    """현재 Frame의 Image와 Camera를 Blender Stencil Pointer에 Commit합니다."""
    import bpy

    frame = int(runtime.scene.frame_current)
    entry = runtime.entries.get(frame)
    if entry is None:
        raise PaintRuntimeError(f"현재 Frame {frame}에 관리 Camera가 없습니다.")
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
    _apply_camera_resolution(runtime, camera, image)
    active_context = context if context is not None else bpy.context

    def apply_source(current_image: Any, image_id: int) -> None:
        """검증된 최신 Image를 Blender 권위 Stencil Pointer에 기록합니다."""
        bind_stencil_source(
            active_context,
            runtime.scene,
            current_image,
            image_id,
            camera,
        )

    if not runtime.cache.commit(
        token,
        image,
        current_frame=int(runtime.scene.frame_current),
        apply=apply_source,
    ):
        raise PaintRuntimeError("최신 Frame Source 요청이 변경되어 연결을 취소했습니다.")
    settings = runtime.scene.projection_paint
    settings.current_image_id = str(entry.image_id)
    settings.current_image_name = str(entry.image_name)
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


def _capture_reconnect_snapshot(context: Any) -> _ReconnectSnapshot:
    """최근접 Camera 적용 전 Scene과 호출 Viewport 상태를 캡처합니다."""

    scene = context.scene
    settings = scene.projection_paint
    return _ReconnectSnapshot(
        frame=int(scene.frame_current),
        scene_camera=scene.camera,
        navigation=capture_viewport_navigation(context),
        manual_stencil_disabled=bool(settings.manual_stencil_disabled),
        stencil_outside_camera_view=bool(settings.stencil_outside_camera_view),
        paint_state=str(settings.paint_state),
    )


def _restore_reconnect_snapshot(
    runtime: PaintRuntime,
    context: Any,
    snapshot: _ReconnectSnapshot,
) -> str:
    """실패한 최근접 Camera 적용을 이전 Frame·Source·Viewport 상태로 되돌립니다."""

    scene = runtime.scene
    settings = scene.projection_paint
    errors: list[str] = []
    runtime.cache.invalidate()
    try:
        if snapshot.frame in runtime.entries:
            activate_managed_frame(
                scene,
                snapshot.frame,
                context,
                refresh_source=False,
            )
            _stage_current_source(runtime, context)
            if snapshot.manual_stencil_disabled or snapshot.stencil_outside_camera_view:
                disconnect_stencil(scene)
            else:
                reconnect_stencil_texture(context)
        else:
            scene.frame_set(snapshot.frame)
    except Exception as exc:
        errors.append(str(exc))
    try:
        scene.camera = snapshot.scene_camera
        restore_viewport_navigation(context, snapshot.navigation)
    except Exception as exc:
        errors.append(str(exc))
    settings.manual_stencil_disabled = snapshot.manual_stencil_disabled
    settings.stencil_outside_camera_view = snapshot.stencil_outside_camera_view
    settings.paint_state = snapshot.paint_state
    if not snapshot.manual_stencil_disabled:
        try:
            start_viewport_tracking(scene)
        except Exception as exc:
            errors.append(str(exc))
    return "; ".join(error for error in errors if error)


def _clear_view_subscription(runtime: PaintRuntime) -> None:
    """Runtime의 Camera View Message Bus 구독을 안전하게 해제합니다."""
    import bpy

    owner = runtime.view_subscription_owner
    runtime.view_subscription_owner = None
    if owner is not None:
        bpy.msgbus.clear_by_owner(owner)


def _camera_view_changed(scene_uuid: str) -> None:
    """Camera View 이탈·복귀를 Blender Pointer 상태에서 즉시 반영합니다."""
    runtime = _RUNTIMES.get(str(scene_uuid))
    if runtime is None:
        return
    if not _projection_runtime_is_active(runtime):
        return
    try:
        session = active_stencil_session(runtime.scene)
        if session is None:
            return
        token = stencil_view_token(session)
        if not bool(token[0]):
            disconnect_stencil(runtime.scene)
        elif (
            bool(runtime.scene.projection_paint.stencil_outside_camera_view)
            and not bool(runtime.scene.projection_paint.manual_stencil_disabled)
        ):
            refresh_stencil(None, session, refresh_viewport=False)
        runtime.viewport_token = stencil_view_token(session)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        runtime.scene.projection_paint.paint_state = "ERROR"


def _paint_context_changed(scene_uuid: str) -> None:
    """Workspace 또는 Object Mode 변경에 따라 Projection Stencil을 동결·재개합니다."""
    import bpy

    runtime = _RUNTIMES.get(str(scene_uuid))
    if runtime is None:
        return
    active = _projection_runtime_is_active(runtime)
    if not active:
        if runtime.projection_workspace_active:
            disconnect_stencil(runtime.scene)
        runtime.projection_workspace_active = False
        runtime.viewport_retry_count = 0
        return
    if runtime.projection_workspace_active:
        return
    runtime.projection_workspace_active = True
    settings = runtime.scene.projection_paint
    if bool(settings.manual_stencil_disabled):
        return
    try:
        _sync_stencil_connection_for_current_view(runtime, bpy.context)
        from .lifecycle import lifecycle_registry

        lifecycle_registry().register_timer(
            _viewport_tracking_tick,
            component="stencil_view_tracking",
            first_interval=VIEWPORT_TRACK_INTERVAL,
        )
    except (AttributeError, ReferenceError, RuntimeError, StencilSessionError, TypeError):
        settings.paint_state = "ERROR"


def _subscribe_camera_view(runtime: PaintRuntime) -> None:
    """Camera View·Workspace·Object Mode 변경을 Scene UUID 기반 Callback에 구독합니다."""
    import bpy

    _clear_view_subscription(runtime)
    owner = object()
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.RegionView3D, "view_perspective"),
        owner=owner,
        args=(runtime.scene_uuid,),
        notify=_camera_view_changed,
    )
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
    """현재 View가 Camera일 때만 Stencil을 연결하고 그 외에는 즉시 해제합니다."""
    session = active_stencil_session(runtime.scene)
    if session is None:
        return False
    if bool(stencil_view_token(session)[0]):
        reconnect_stencil_texture(context, session)
        connected = True
    else:
        disconnect_stencil(runtime.scene)
        connected = False
    runtime.viewport_token = stencil_view_token(session)
    return connected


def _track_runtime(runtime: PaintRuntime) -> float | None:
    """한 Scene Runtime의 View Token을 비교하고 변경된 Stencil만 갱신합니다."""
    import bpy

    settings = runtime.scene.projection_paint
    if not _projection_runtime_is_active(runtime):
        if runtime.projection_workspace_active:
            disconnect_stencil(runtime.scene)
            runtime.projection_workspace_active = False
        return None
    if not runtime.projection_workspace_active:
        runtime.projection_workspace_active = True
        if not bool(settings.manual_stencil_disabled):
            _sync_stencil_connection_for_current_view(runtime, bpy.context)
    if bool(settings.manual_stencil_disabled):
        return None
    if not _frame_source_is_current(runtime):
        runtime.viewport_retry_count = 0
        return VIEWPORT_TRACK_INTERVAL
    session = active_stencil_session(runtime.scene)
    if session is None:
        return None
    try:
        token = stencil_view_token(session)
        if token == runtime.viewport_token:
            runtime.viewport_retry_count = 0
            return VIEWPORT_TRACK_INTERVAL if bool(token[0]) else OUTSIDE_CAMERA_VIEW_INTERVAL
        runtime.viewport_token = token
        if not bool(token[0]):
            disconnect_stencil(runtime.scene)
            runtime.viewport_token = stencil_view_token(session)
            runtime.viewport_retry_count = 0
            return OUTSIDE_CAMERA_VIEW_INTERVAL
        if not refresh_stencil(None, session, refresh_viewport=False):
            return None
        runtime.viewport_token = stencil_view_token(session)
        runtime.viewport_retry_count = 0
        return VIEWPORT_TRACK_INTERVAL
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


def start_viewport_tracking(scene: Any | None = None) -> None:
    """현재 Scene Runtime의 Message Bus와 단일 View Timer를 시작합니다."""
    import bpy

    active_scene = scene if scene is not None else getattr(bpy.context, "scene", None)
    if active_scene is None:
        return
    runtime = ensure_paint_runtime(active_scene)
    session = active_stencil_session(active_scene)
    if (
        session is None
        or not _projection_runtime_is_active(runtime)
        or bool(active_scene.projection_paint.manual_stencil_disabled)
    ):
        return
    runtime.viewport_token = stencil_view_token(session)
    runtime.projection_workspace_active = True
    _subscribe_camera_view(runtime)
    from .lifecycle import lifecycle_registry

    lifecycle_registry().register_timer(
        _viewport_tracking_tick,
        component="stencil_view_tracking",
        first_interval=VIEWPORT_TRACK_INTERVAL,
    )


def start_paint_runtime(
    context: Any,
    paint_session: Any = None,
    *,
    reconnect_on_start: bool = True,
) -> PaintRuntime:
    """Blender Paint 상태에서 Scene별 Frame Cache를 생성하고 Source를 연결합니다."""
    scene = getattr(paint_session, "scene", None) or context.scene
    runtime = ensure_paint_runtime(scene)
    try:
        _stage_current_source(runtime, context)
        if reconnect_on_start:
            reconnect_stencil_texture(context)
            start_viewport_tracking(scene)
    except Exception:
        stop_paint_runtime(context, scene=scene, restore_resolution=False)
        raise
    return runtime


def refresh_frame_source(scene: Any) -> bool:
    """Frame 변경 시 Blender 활성 의도를 확인하고 Source와 Stencil을 갱신합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return False
    try:
        runtime = ensure_paint_runtime(scene)
        _stage_current_source(runtime)
        if not _projection_runtime_is_active(runtime):
            return True
        session = active_stencil_session(scene)
        if session is None:
            return False
        if not bool(stencil_view_token(session)[0]):
            disconnect_stencil(scene)
            start_viewport_tracking(scene)
            return True
        refreshed = refresh_stencil(None, session)
        if refreshed:
            start_viewport_tracking(scene)
        return refreshed
    except Exception as exc:
        settings.status = f"Frame Source 갱신 실패: {exc}"
        settings.status_kind = "ERROR"
        settings.paint_state = "ERROR"
        return False


def reconnect_current_frame(context: Any) -> int:
    """Runtime 존재 여부와 무관하게 현재 Frame Source를 Stencil로 복구합니다."""
    scene = context.scene
    try:
        from .overlay_migration import repair_managed_overlay_if_needed

        repair_managed_overlay_if_needed(context)
    except RuntimeError as exc:
        raise PaintRuntimeError(f"관리 Overlay UDIM 복구 실패: {exc}") from exc
    runtime = ensure_paint_runtime(scene)
    if not _projection_runtime_is_active(runtime):
        raise PaintRuntimeError("전용 작업공간의 실제 Texture Paint Target이 아닙니다.")
    image_id = _stage_current_source(runtime, context)
    reconnect_stencil_texture(context)
    start_viewport_tracking(scene)
    return image_id


def resume_stencil_after_layer_selection(context: Any) -> bool:
    """Camera View의 실제 Paint Runtime에서 Layer 선택 뒤 Projection Stencil을 복구합니다."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    runtime = active_paint_runtime(scene)
    if runtime is None or not _projection_runtime_is_active(runtime):
        return False
    try:
        settings = getattr(scene, "projection_paint", None)
        if (
            settings is None
            or _pointer(settings.stencil_source_image) == 0
            or int(settings.active_image_id) <= 0
            or not _frame_source_is_current(runtime)
        ):
            return False
        session = active_stencil_session(scene)
        if session is None:
            return False
        if not bool(stencil_view_token(session)[0]):
            return False
        reconnect_stencil_texture(context, session)
        brush = settings.stencil_brush
        texture = settings.stencil_texture
        if (
            _pointer(brush) == 0
            or _pointer(texture) == 0
            or brush.texture is not texture
            or brush.texture_slot.map_mode != "STENCIL"
        ):
            raise PaintRuntimeError("관리 Stencil Brush 상태를 복구하지 못했습니다.")
        scene.tool_settings.image_paint.unified_paint_settings.use_unified_color = False
        brush.color = (1.0, 1.0, 1.0)
        runtime.viewport_token = stencil_view_token(session)
        start_viewport_tracking(scene)
        return True
    except (
        AttributeError,
        ReferenceError,
        RuntimeError,
        StencilSessionError,
        TypeError,
        ValueError,
    ):
        # Layer 선택 Transaction은 완료했으므로 Stencil 복구 실패로 되돌리지 않습니다.
        return False


def reconnect_nearest_camera(context: Any) -> CameraCandidate:
    """현재 Viewport 시점에 가장 적합한 Camera Frame과 Stencil Source를 연결합니다."""

    scene = context.scene
    settings = scene.projection_paint
    try:
        from .overlay_migration import repair_managed_overlay_if_needed

        repair_managed_overlay_if_needed(context)
    except RuntimeError as exc:
        raise PaintRuntimeError(f"관리 Overlay UDIM 복구 실패: {exc}") from exc
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
    _camera_for_entry(runtime, candidate.entry)
    mutated = False
    try:
        mutated = True
        activate_managed_frame(
            scene,
            candidate.entry.frame,
            context,
            refresh_source=False,
        )
        image_id = _stage_current_source(runtime, context)
        if int(image_id) != int(candidate.entry.image_id):
            raise PaintRuntimeError("선택한 Camera와 연결된 IMAGE_ID가 일치하지 않습니다.")
        reconnect_stencil_texture(context)
        start_viewport_tracking(scene)
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

        if _pointer(scene) != scene_pointer:
            return None
        from .paint_state import reconcile_paint_state

        snapshot = reconcile_paint_state(bpy.context, reason)
        if snapshot.scene is scene and snapshot.state in {"ACTIVE", "FROZEN", "MANUAL_DISABLED"}:
            ensure_paint_runtime(scene)
            if snapshot.state == "ACTIVE":
                reconnect_current_frame(bpy.context)
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
    """현재 Scene의 Callback과 Source Cache를 닫고 필요 시 Render 크기를 복구합니다."""
    import bpy

    active_scene = scene or getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    if active_scene is None:
        return
    key = str(active_scene.get("pp_scene_uuid", "") or "")
    runtime = _RUNTIMES.pop(key, None)
    if runtime is not None:
        _clear_view_subscription(runtime)
        runtime.cache.close()
    if not _RUNTIMES:
        from .lifecycle import lifecycle_registry

        lifecycle_registry().unregister_timer(_viewport_tracking_tick)
    settings = getattr(active_scene, "projection_paint", None)
    if settings is None or not restore_resolution:
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
    "start_viewport_tracking",
    "stop_paint_runtime",
]
