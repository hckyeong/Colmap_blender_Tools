"""현재 프레임의 COLMAP 이미지를 카메라 배경 슬롯에 연결합니다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bpy

from .camera_scene import (
    CameraManifestEntry,
    apply_projection_camera_state,
    projection_camera_session_is_active,
    runtime_for_scene,
    scene_manifest,
)
from .ownership import (
    ensure_scene_uuid,
    is_owned,
    tag_owned,
)


REVIEW_IMAGE_KIND = "CAMERA_REVIEW_IMAGE"
_TRACKED_SLOTS: dict[str, tuple[int, int]] = {}


class CameraReviewError(RuntimeError):
    """카메라 배경 이미지 연결 계약 위반을 나타냅니다."""


def _pointer(value: Any) -> int:
    """삭제된 Blender RNA 객체를 안전하게 0으로 변환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def _settings(scene: Any) -> Any | None:
    """Scene의 Projection Painting 설정을 예외 없이 반환합니다."""

    try:
        return getattr(scene, "projection_paint", None)
    except (AttributeError, ReferenceError, RuntimeError):
        return None


def _current_entry(scene: Any) -> CameraManifestEntry | None:
    """현재 Frame에 연결된 Camera Manifest 항목을 반환합니다."""

    runtime = runtime_for_scene(scene)
    if runtime is not None:
        return runtime.entries_by_frame.get(int(scene.frame_current))
    manifest = scene_manifest(scene)
    if manifest is None:
        return None
    return next(
        (
            entry
            for entry in manifest.entries
            if int(entry.frame) == int(scene.frame_current)
        ),
        None,
    )


def _camera_for_entry(scene: Any, entry: CameraManifestEntry) -> Any | None:
    """Manifest 항목에 대응하는 유효한 Camera Object를 반환합니다."""

    runtime = runtime_for_scene(scene)
    camera = runtime.objects.get(entry.camera_uuid) if runtime is not None else None
    if camera is not None and _pointer(camera):
        return camera
    active = getattr(scene, "camera", None)
    if active is not None and _pointer(active):
        return active
    return None


def _image_path(scene: Any, image_id: int) -> Path:
    """영구 Image Path Manifest에서 IMAGE_ID의 실제 파일 경로를 반환합니다."""

    settings = _settings(scene)
    if settings is None:
        raise CameraReviewError("Projection Paint Scene 설정이 없습니다.")
    try:
        manifest = json.loads(str(settings.image_path_manifest_json))
        raw_path = str(manifest[str(int(image_id))])
        path = Path(raw_path).expanduser().resolve()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CameraReviewError(
            f"IMAGE_ID {image_id}의 Source Image 경로가 없습니다."
        ) from exc
    if not path.is_file():
        raise CameraReviewError(f"Source Image 파일이 없습니다: {path}")
    return path


def _redraw_viewports(context: Any = None) -> None:
    """열린 모든 3D Viewport에 카메라 배경 변경을 다시 그리도록 요청합니다."""

    active_context = context if context is not None else bpy.context
    window_manager = getattr(active_context, "window_manager", None)
    for window in tuple(getattr(window_manager, "windows", ())):
        for area in tuple(window.screen.areas):
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _viewport_context(context: Any) -> tuple[Any, Any, Any]:
    """현재 Operator Context에서 3D Viewport와 RegionView3D를 검증해 반환합니다."""

    area = getattr(context, "area", None)
    space = getattr(context, "space_data", None)
    rv3d = getattr(space, "region_3d", None)
    if (
        area is None
        or area.type != "VIEW_3D"
        or space is None
        or getattr(space, "type", "") != "VIEW_3D"
        or rv3d is None
    ):
        raise CameraReviewError("현재 영역이 3D Viewport가 아닙니다.")
    return area, space, rv3d


def _viewport_available(context: Any) -> bool:
    """Camera Review 버튼을 실행할 수 있는 3D Viewport Context인지 확인합니다."""

    try:
        _viewport_context(context)
        return True
    except CameraReviewError:
        return False


def _projection_workspace_active(context: Any) -> bool:
    """현재 Window가 Scene에 연결된 Projection Paint Workspace인지 확인합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "projection_paint", None)
    window = getattr(context, "window", None)
    workspace = getattr(settings, "paint_workspace", None)
    return bool(
        window is not None
        and workspace is not None
        and getattr(window, "workspace", None) is workspace
    )


def _camera_view_active(context: Any) -> bool:
    """Projection Paint Workspace의 현재 3D Viewport가 Camera View인지 확인합니다."""

    area = getattr(context, "area", None)
    space = getattr(context, "space_data", None)
    region_3d = getattr(space, "region_3d", None)
    return bool(
        _projection_workspace_active(context)
        and area is not None
        and area.type == "VIEW_3D"
        and region_3d is not None
        and region_3d.view_perspective == "CAMERA"
    )


def _apply_camera_frame(scene: Any, camera: Any) -> None:
    """COLMAP Camera 해상도를 Scene Camera와 Render Frame에 적용합니다."""

    width, height = camera_dimensions(camera.data)
    if width <= 0 or height <= 0:
        raise CameraReviewError("현재 Camera의 COLMAP 해상도 Metadata가 없습니다.")
    apply_projection_camera_state(
        scene,
        camera,
        render_resolution=(width, height, 100),
    )


def _activate_viewport_camera(context: Any, camera: Any) -> None:
    """호출된 3D Viewport를 지정 Camera View로 전환합니다."""

    area, space, rv3d = _viewport_context(context)
    if not projection_camera_session_is_active(
        context.scene,
        window=getattr(context, "window", None),
    ):
        raise CameraReviewError("활성 Projection Paint Camera 세션이 아닙니다.")
    space.use_local_camera = True
    space.camera = camera
    rv3d.view_perspective = "CAMERA"
    area.tag_redraw()


def _slot_by_pointer(camera_data: Any, pointer: int) -> Any | None:
    """Camera Data에서 지정 포인터와 일치하는 배경 슬롯을 반환합니다."""

    if not pointer:
        return None
    return next(
        (
            slot
            for slot in camera_data.background_images
            if _pointer(slot) == int(pointer)
        ),
        None,
    )


def _persistent_slot(settings: Any, camera_data: Any) -> Any | None:
    """저장된 Camera Data와 Index가 현재 Review 슬롯과 일치할 때만 반환합니다."""

    if camera_data is None or settings.review_background_camera_data is not camera_data:
        return None
    index = int(settings.review_background_slot_index)
    if (
        index < 0
        or index >= len(camera_data.background_images)
    ):
        return None
    slot = camera_data.background_images[index]
    expected = settings.review_background_image
    if expected is None or slot.image is not expected:
        return None
    return slot


def _managed_slot(scene: Any) -> tuple[Any | None, Any | None]:
    """Runtime 포인터를 우선하고 저장된 Blender 상태로 현재 Review 슬롯을 복구합니다."""

    settings = _settings(scene)
    if settings is None:
        return None, None
    camera_data = settings.review_background_camera_data
    if camera_data is None or not _pointer(camera_data):
        return None, None
    scene_key = ensure_scene_uuid(scene)
    tracked = _TRACKED_SLOTS.get(scene_key)
    if tracked is not None and tracked[0] == _pointer(camera_data):
        slot = _slot_by_pointer(camera_data, tracked[1])
        if slot is not None:
            return camera_data, slot
    slot = _persistent_slot(settings, camera_data)
    if slot is not None:
        _TRACKED_SLOTS[scene_key] = (_pointer(camera_data), _pointer(slot))
        return camera_data, slot
    return camera_data, None


def _forget_slot(scene: Any, camera_data: Any) -> None:
    """Scene Runtime의 Review 슬롯 추적만 제거합니다."""

    _TRACKED_SLOTS.pop(ensure_scene_uuid(scene), None)
    del camera_data


def _remove_managed_slot(scene: Any) -> None:
    """정확히 추적된 Review 배경 슬롯만 Camera Data에서 제거합니다."""

    settings = _settings(scene)
    if settings is None:
        return
    camera_data, slot = _managed_slot(scene)
    if slot is not None:
        try:
            slot.show_background_image = False
            camera_data.background_images.remove(slot)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    _forget_slot(scene, camera_data)
    settings.review_background_camera_data = None
    settings.review_background_slot_index = -1


def _ensure_managed_slot(scene: Any, camera_data: Any) -> Any:
    """현재 Camera Data에 Review 전용 배경 슬롯 하나를 생성하거나 재사용합니다."""

    settings = _settings(scene)
    if settings is None:
        raise CameraReviewError("Projection Paint Scene 설정이 없습니다.")
    previous_data, slot = _managed_slot(scene)
    if previous_data is not camera_data:
        _remove_managed_slot(scene)
        slot = None
    if slot is None:
        slot = camera_data.background_images.new()
    index = next(
        (
            index
            for index, candidate in enumerate(camera_data.background_images)
            if _pointer(candidate) == _pointer(slot)
        ),
        -1,
    )
    if index < 0:
        raise CameraReviewError("Camera Review 배경 슬롯을 확인할 수 없습니다.")
    settings.review_background_camera_data = camera_data
    settings.review_background_slot_index = index
    _TRACKED_SLOTS[ensure_scene_uuid(scene)] = (
        _pointer(camera_data),
        _pointer(slot),
    )
    return slot


def _matching_review_image(settings: Any, image_id: int, path: Path) -> Any | None:
    """현재 Review Image가 요청한 IMAGE_ID와 파일 경로에 일치하면 반환합니다."""

    image = settings.review_background_image
    if (
        int(settings.review_background_image_id) != int(image_id)
        or image is None
        or not _pointer(image)
    ):
        return None
    try:
        current_path = Path(bpy.path.abspath(image.filepath)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return image if current_path == path else None


def _load_review_image(scene: Any, entry: CameraManifestEntry, path: Path) -> Any:
    """사용자 Image와 분리된 현재 프레임 Review Image를 로드합니다."""

    settings = _settings(scene)
    if settings is None:
        raise CameraReviewError("Projection Paint Scene 설정이 없습니다.")
    stencil_image = settings.stencil_source_image
    if (
        int(settings.active_image_id) == int(entry.image_id)
        and stencil_image is not None
        and _pointer(stencil_image)
    ):
        return stencil_image
    try:
        image = bpy.data.images.load(str(path), check_existing=False)
    except RuntimeError as exc:
        raise CameraReviewError(f"Source Image를 열 수 없습니다: {path}") from exc
    tag_owned(
        image,
        REVIEW_IMAGE_KIND,
        dataset_uuid=str(settings.dataset_uuid),
        record_id=entry.image_id,
        source_fingerprint=str(settings.source_fingerprint),
    )
    return image


def _validate_image_size(camera: Any, image: Any) -> None:
    """Review Image 해상도가 COLMAP Camera 해상도와 일치하는지 검증합니다."""

    expected = camera_dimensions(camera.data)
    actual = tuple(int(value) for value in image.size)
    if min(actual, default=0) <= 0 or actual != expected:
        raise CameraReviewError(
            f"Camera 해상도 {expected}와 Source Image 해상도 {actual}가 일치하지 않습니다."
        )


def _release_review_image(image: Any) -> None:
    """참조가 사라진 애드온 소유 Review Image만 안전하게 제거합니다."""

    if image is None or not _pointer(image):
        return
    if not is_owned(image, kind=REVIEW_IMAGE_KIND) or int(image.users) != 0:
        return
    try:
        image.gl_free()
        image.buffers_free()
        bpy.data.images.remove(image)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return


def update_camera_review_style(scene: Any, context: Any = None) -> bool:
    """현재 Review 슬롯의 표시 여부와 Alpha만 변경합니다."""

    settings = _settings(scene)
    if settings is None or not projection_camera_session_is_active(scene):
        return False
    camera_data, slot = _managed_slot(scene)
    if slot is None:
        return False
    slot.alpha = float(settings.review_background_alpha)
    slot.display_depth = "FRONT"
    slot.frame_method = "FIT"
    slot.show_background_image = bool(settings.review_background_enabled)
    camera_data.show_background_images = True
    camera_data.update_tag()
    _redraw_viewports(context)
    return True


def sync_camera_review(scene: Any, context: Any = None) -> bool:
    """현재 Frame의 IMAGE_ID를 카메라 배경 이미지로 원자적으로 연결합니다."""

    settings = _settings(scene)
    entry = _current_entry(scene)
    if (
        settings is None
        or entry is None
        or not projection_camera_session_is_active(scene)
    ):
        return False
    settings.current_image_id = str(entry.image_id)
    settings.current_image_name = str(entry.image_name)
    if not bool(settings.review_background_enabled):
        update_camera_review_style(scene, context)
        return False
    camera = _camera_for_entry(scene, entry)
    if camera is None or getattr(camera, "type", "") != "CAMERA":
        raise CameraReviewError(f"IMAGE_ID {entry.image_id}의 Camera Object가 없습니다.")
    path = _image_path(scene, entry.image_id)
    image = _matching_review_image(settings, entry.image_id, path)
    loaded = False
    if image is None:
        image = _load_review_image(scene, entry, path)
        loaded = image is not settings.stencil_source_image
    try:
        _validate_image_size(camera, image)
        old_image = settings.review_background_image
        slot = _ensure_managed_slot(scene, camera.data)
        slot.image = image
        settings.review_background_image = image
        settings.review_background_image_id = int(entry.image_id)
        update_camera_review_style(scene, context)
        if old_image is not image:
            _release_review_image(old_image)
        return True
    except Exception:
        if loaded and image is not settings.review_background_image:
            _release_review_image(image)
        raise


def look_through_current_camera(context: Any) -> int:
    """현재 Frame의 관리 Camera를 활성화하고 호출된 Viewport를 Camera View로 전환합니다."""

    _viewport_context(context)
    return initialize_camera_review(context)


def initialize_camera_review(context: Any) -> int:
    """Import 직후 현재 Camera·배경·Render Frame과 가능한 Viewport를 초기화합니다."""

    scene = getattr(context, "scene", None)
    entry = _current_entry(scene) if scene is not None else None
    if (
        scene is None
        or entry is None
        or not projection_camera_session_is_active(
            scene,
            window=getattr(context, "window", None),
        )
    ):
        raise CameraReviewError("현재 Frame에 관리 Camera가 없습니다.")
    camera = _camera_for_entry(scene, entry)
    if camera is None or getattr(camera, "type", "") != "CAMERA":
        raise CameraReviewError(f"IMAGE_ID {entry.image_id}의 Camera Object가 없습니다.")
    sync_camera_review(scene, context)
    _apply_camera_frame(scene, camera)
    if _viewport_available(context):
        _activate_viewport_camera(context, camera)
    else:
        _redraw_viewports(context)
    return int(entry.image_id)


def update_camera_review_from_settings(settings: Any, context: Any) -> None:
    """UI 토글과 Alpha 변경을 현재 Scene의 Review 배경에 즉시 반영합니다."""

    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None or not projection_camera_session_is_active(
        scene,
        window=getattr(context, "window", None),
    ):
        return
    try:
        if bool(settings.review_background_enabled):
            sync_camera_review(scene, context)
        else:
            update_camera_review_style(scene, context)
    except CameraReviewError as exc:
        settings.status = f"Camera Review 갱신 실패: {exc}"
        settings.status_kind = "ERROR"


def refresh_camera_review(scene: Any, context: Any = None) -> bool:
    """Handler 경계에서 Review 오류를 상태로 격리하고 성공 여부를 반환합니다."""

    if not projection_camera_session_is_active(scene):
        return False
    try:
        return sync_camera_review(scene, context)
    except CameraReviewError as exc:
        settings = _settings(scene)
        if settings is not None:
            settings.status = f"Camera Review 갱신 실패: {exc}"
            settings.status_kind = "WARNING"
        return False


def clear_camera_review_runtime() -> None:
    """Load·Undo·Redo 뒤 Blender 데이터는 유지하고 슬롯 포인터 Cache만 비웁니다."""

    _TRACKED_SLOTS.clear()


def camera_dimensions(camera_data: Any) -> tuple[int, int]:
    """공용 또는 Legacy Camera Data에서 검증 가능한 COLMAP 해상도를 반환합니다."""

    try:
        width = int(
            camera_data.get(
                "colmap_width",
                camera_data.get("pp_camera_width", 0),
            )
        )
        height = int(
            camera_data.get(
                "colmap_height",
                camera_data.get("pp_camera_height", 0),
            )
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0, 0
    return width, height


def release_camera_review_links(scene: Any) -> None:
    """Dataset 교체 전에 PP 배경 슬롯과 현재 Image 연결만 안전하게 해제합니다."""

    settings = _settings(scene)
    if settings is None:
        return
    image = settings.review_background_image
    _remove_managed_slot(scene)
    settings.review_background_image = None
    settings.review_background_image_id = 0
    settings.current_image_id = ""
    settings.current_image_name = ""
    if image is not settings.stencil_source_image:
        _release_review_image(image)


class PP_OT_review_look_through(bpy.types.Operator):
    """현재 Frame의 COLMAP Camera를 현재 3D Viewport에서 표시합니다."""

    bl_idname = "projection_paint.review_look_through"
    bl_label = "Look Through Camera"
    bl_description = "현재 3D Viewport를 현재 프레임의 COLMAP Camera View로 전환합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: Any) -> bool:
        """현재 Frame에 관리 Camera가 있고 3D Viewport일 때만 버튼을 활성화합니다."""

        from .snapshot_update import snapshot_is_ready

        scene = getattr(context, "scene", None)
        return bool(
            scene is not None
            and snapshot_is_ready(scene)
            and _current_entry(scene) is not None
            and _viewport_available(context)
            and projection_camera_session_is_active(
                scene,
                window=getattr(context, "window", None),
            )
        )

    def execute(self, context: Any) -> set[str]:
        """현재 Viewport를 관리 Camera 시점으로 전환하고 결과를 보고합니다."""

        try:
            image_id = look_through_current_camera(context)
        except CameraReviewError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings = context.scene.projection_paint
        settings.status = f"IMAGE_ID {image_id} Camera View를 표시합니다."
        settings.status_kind = "SUCCESS"
        return {"FINISHED"}


class PP_OT_review_background_toggle(bpy.types.Operator):
    """Camera View에서 Camera Review 배경 표시를 전환합니다."""

    bl_idname = "projection_paint.review_background_toggle"
    bl_label = "Toggle Background Image"
    bl_description = "Camera View에서 Camera Review 배경 이미지를 켜거나 끕니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: Any) -> bool:
        """Projection Paint Workspace의 Camera View에서만 단축키를 허용합니다."""

        from .snapshot_update import snapshot_is_ready

        scene = getattr(context, "scene", None)
        return bool(
            scene is not None
            and snapshot_is_ready(scene)
            and _camera_view_active(context)
            and projection_camera_session_is_active(
                scene,
                window=getattr(context, "window", None),
            )
        )

    def execute(self, context: Any) -> set[str]:
        """설정값 반전으로 기존 Camera Review 갱신 경로를 실행합니다."""

        settings = context.scene.projection_paint
        settings.review_background_enabled = not bool(
            settings.review_background_enabled
        )
        state = "ON" if settings.review_background_enabled else "OFF"
        self.report({"INFO"}, f"Background Image: {state}")
        return {"FINISHED"}


CLASSES = (PP_OT_review_look_through, PP_OT_review_background_toggle)


__all__ = [
    "CameraReviewError",
    "CLASSES",
    "PP_OT_review_look_through",
    "PP_OT_review_background_toggle",
    "REVIEW_IMAGE_KIND",
    "camera_dimensions",
    "clear_camera_review_runtime",
    "initialize_camera_review",
    "look_through_current_camera",
    "refresh_camera_review",
    "release_camera_review_links",
    "sync_camera_review",
    "update_camera_review_from_settings",
    "update_camera_review_style",
]
