"""Camera Review의 Viewport와 Render 임시 상태를 저장하고 안전하게 복원합니다."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ViewportState:
    view_perspective: str
    view_location: object
    view_rotation: object
    view_distance: float
    view_camera_zoom: float
    view_camera_offset: object
    viewport_camera: object


@dataclass
class ViewportSnapshot:
    window_pointer: int
    area_pointer: int
    region_pointer: int
    area: object
    region: object
    space: object
    rv3d: object
    previous: ViewportState
    last_applied: ViewportState | None = None


@dataclass(frozen=True)
class RenderState:
    resolution_x: int
    resolution_y: int
    resolution_percentage: int
    pixel_aspect_x: float
    pixel_aspect_y: float
    scene_camera: object


def _pointer(value) -> int:
    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _copy_value(value):
    copier = getattr(value, "copy", None)
    return copier() if callable(copier) else value


def _viewport_state(space, rv3d) -> ViewportState:
    return ViewportState(
        view_perspective=rv3d.view_perspective,
        view_location=_copy_value(rv3d.view_location),
        view_rotation=_copy_value(rv3d.view_rotation),
        view_distance=float(rv3d.view_distance),
        view_camera_zoom=float(rv3d.view_camera_zoom),
        view_camera_offset=_copy_value(rv3d.view_camera_offset),
        viewport_camera=space.camera,
    )


def _render_state(scene) -> RenderState:
    render = scene.render
    return RenderState(
        resolution_x=int(render.resolution_x),
        resolution_y=int(render.resolution_y),
        resolution_percentage=int(render.resolution_percentage),
        pixel_aspect_x=float(render.pixel_aspect_x),
        pixel_aspect_y=float(render.pixel_aspect_y),
        scene_camera=scene.camera,
    )


def _viewport_key(context, region) -> tuple[int, int, int]:
    return (
        _pointer(getattr(context, "window", None)),
        _pointer(getattr(context, "area", None)),
        _pointer(region),
    )


def _require_viewport(context):
    area = getattr(context, "area", None)
    space = getattr(context, "space_data", None)
    if area is None or area.type != "VIEW_3D":
        raise ValueError("현재 영역이 3D Viewport가 아닙니다.")
    context_region = getattr(context, "region", None)
    region = (
        context_region
        if context_region is not None and context_region.type == "WINDOW"
        else next(
            (
                candidate
                for candidate in getattr(area, "regions", ())
                if candidate.type == "WINDOW"
            ),
            None,
        )
    )
    if region is None:
        raise ValueError("현재 3D Viewport의 WINDOW region을 찾을 수 없습니다.")
    rv3d = getattr(space, "region_3d", None)
    if rv3d is None:
        raise ValueError("현재 3D Viewport의 RegionView3D를 찾을 수 없습니다.")
    return area, region, space, rv3d


def _snapshot_valid(snapshot: ViewportSnapshot) -> bool:
    return (
        _pointer(snapshot.area) == snapshot.area_pointer
        and _pointer(snapshot.region) == snapshot.region_pointer
    )


def _pixel_aspect_axes(camera_object) -> tuple[float, float]:
    """Blender의 축별 최솟값을 지키면서 COLMAP의 y/x 비율을 보존합니다."""
    raw_ratio = camera_object.data.get(
        "colmap_pixel_aspect_y_over_x",
        1.0,
    )
    try:
        ratio = float(raw_ratio)
    except (TypeError, ValueError) as exc:
        raise ValueError("Camera의 pixel aspect ratio가 숫자가 아닙니다.") from exc
    if not math.isfinite(ratio) or ratio <= 0.0:
        raise ValueError(
            "Camera의 pixel aspect ratio는 0보다 큰 유한한 값이어야 합니다."
        )
    if ratio >= 1.0:
        return 1.0, ratio
    return 1.0 / ratio, 1.0


def _apply_render_for_entry(scene, camera_object, entry) -> None:
    pixel_aspect_x, pixel_aspect_y = _pixel_aspect_axes(camera_object)
    render = scene.render
    render.resolution_x = entry.width
    render.resolution_y = entry.height
    render.resolution_percentage = 100
    render.pixel_aspect_x = pixel_aspect_x
    render.pixel_aspect_y = pixel_aspect_y
    scene.camera = camera_object


def _apply_viewport_camera(snapshot: ViewportSnapshot, camera_object) -> None:
    snapshot.space.camera = camera_object
    snapshot.rv3d.view_perspective = "CAMERA"
    snapshot.last_applied = _viewport_state(snapshot.space, snapshot.rv3d)
    snapshot.area.tag_redraw()


def look_through_current(context, session) -> None:
    """호출된 현재 Viewport만 저장하고 Review Camera View로 전환합니다."""
    area, region, space, rv3d = _require_viewport(context)
    if session.current_image_id is None:
        raise ValueError("현재 Camera Review 항목이 없습니다.")
    camera_object = session.camera_objects[session.current_image_id]
    entry = session.review_index.entries[session.current_index]
    # 잘못된 비율은 snapshot이나 Render 상태를 만들기 전에 거부합니다.
    _pixel_aspect_axes(camera_object)
    key = _viewport_key(context, region)

    snapshot = session.viewport_snapshots.get(key)
    if snapshot is None:
        snapshot = ViewportSnapshot(
            window_pointer=key[0],
            area_pointer=key[1],
            region_pointer=key[2],
            area=area,
            region=region,
            space=space,
            rv3d=rv3d,
            previous=_viewport_state(space, rv3d),
        )
        session.viewport_snapshots[key] = snapshot
    if session.render_snapshot is None:
        session.render_snapshot = _render_state(context.scene)

    _apply_render_for_entry(context.scene, camera_object, entry)
    session.render_last_applied = _render_state(context.scene)
    _apply_viewport_camera(snapshot, camera_object)
    settings = context.scene.colmap_import
    settings.review_restore_available = True
    settings.review_status = f"IMAGE_ID {entry.image_id} Camera View를 표시합니다."


def update_review_view_for_current(context, session) -> None:
    """Look Through가 활성일 때만 Scene Camera, Render와 활성 Viewport를 갱신합니다."""
    if not session.viewport_snapshots or session.current_image_id is None:
        return

    invalid_keys: list[tuple[int, int, int]] = []
    for key, snapshot in session.viewport_snapshots.items():
        if not _snapshot_valid(snapshot):
            invalid_keys.append(key)
    for key in invalid_keys:
        session.viewport_snapshots.pop(key, None)

    settings = context.scene.colmap_import
    if not session.viewport_snapshots:
        render_restored, camera_restored = _restore_render_if_unchanged(session)
        settings.review_restore_available = False
        if render_restored and camera_restored:
            settings.review_status = (
                "닫힌 Viewport의 Camera Review override를 복원했습니다."
            )
        else:
            settings.review_status = (
                "닫힌 Viewport 정리에서 Review 이후 사용자 변경을 보존했습니다."
            )
        return

    entry = session.review_index.entries[session.current_index]
    camera_object = session.camera_objects[session.current_image_id]
    _apply_render_for_entry(context.scene, camera_object, entry)
    session.render_last_applied = _render_state(context.scene)
    for snapshot in session.viewport_snapshots.values():
        _apply_viewport_camera(snapshot, camera_object)
    settings.review_restore_available = True


def _viewport_matches_applied(snapshot: ViewportSnapshot) -> bool:
    if snapshot.last_applied is None:
        return False
    return _viewport_state(snapshot.space, snapshot.rv3d) == snapshot.last_applied


def _restore_viewport_snapshot(snapshot: ViewportSnapshot) -> bool:
    if not _snapshot_valid(snapshot) or not _viewport_matches_applied(snapshot):
        return False
    previous = snapshot.previous
    rv3d = snapshot.rv3d
    snapshot.space.camera = previous.viewport_camera
    rv3d.view_location = _copy_value(previous.view_location)
    rv3d.view_rotation = _copy_value(previous.view_rotation)
    rv3d.view_distance = previous.view_distance
    rv3d.view_camera_zoom = previous.view_camera_zoom
    rv3d.view_camera_offset = _copy_value(previous.view_camera_offset)
    rv3d.view_perspective = previous.view_perspective
    snapshot.area.tag_redraw()
    return True


def _restore_render_if_unchanged(session) -> tuple[bool, bool]:
    """Render 값과 Scene.camera를 각각 마지막 적용값일 때만 복원합니다."""
    scene = session.scene
    previous = session.render_snapshot
    applied = session.render_last_applied
    if previous is None or applied is None:
        return True, True

    current = _render_state(scene)
    render_unchanged = (
        current.resolution_x,
        current.resolution_y,
        current.resolution_percentage,
        current.pixel_aspect_x,
        current.pixel_aspect_y,
    ) == (
        applied.resolution_x,
        applied.resolution_y,
        applied.resolution_percentage,
        applied.pixel_aspect_x,
        applied.pixel_aspect_y,
    )
    camera_unchanged = current.scene_camera is applied.scene_camera
    if render_unchanged:
        render = scene.render
        render.resolution_x = previous.resolution_x
        render.resolution_y = previous.resolution_y
        render.resolution_percentage = previous.resolution_percentage
        render.pixel_aspect_x = previous.pixel_aspect_x
        render.pixel_aspect_y = previous.pixel_aspect_y
    if camera_unchanged:
        scene.camera = previous.scene_camera
    session.render_snapshot = None
    session.render_last_applied = None
    return render_unchanged, camera_unchanged


def restore_previous_view(context, session) -> tuple[str, ...]:
    """현재 Viewport snapshot과 마지막 Viewport가 끝날 때 Render 상태를 복원합니다."""
    _, region, _, _ = _require_viewport(context)
    key = _viewport_key(context, region)
    snapshot = session.viewport_snapshots.pop(key, None)
    if snapshot is None:
        raise ValueError("현재 Viewport에 복원할 Camera Review snapshot이 없습니다.")

    warnings: list[str] = []
    if not _restore_viewport_snapshot(snapshot):
        warnings.append("Viewport의 사용자 변경 또는 유효하지 않은 snapshot을 보존했습니다.")
    if not session.viewport_snapshots:
        render_restored, camera_restored = _restore_render_if_unchanged(session)
        if not render_restored:
            warnings.append("Render의 사용자 변경을 보존했습니다.")
        if not camera_restored:
            warnings.append("Scene Camera의 사용자 변경을 보존했습니다.")

    settings = context.scene.colmap_import
    settings.review_restore_available = bool(session.viewport_snapshots)
    settings.review_status = (
        "Camera Review 이후 사용자 변경을 보존했습니다: " + " ".join(warnings)
        if warnings
        else "이전 Viewport와 Render 설정을 복원했습니다."
    )
    return tuple(warnings)


def restore_session_views(session) -> tuple[str, ...]:
    """파일 전환·undo·unregister에서 남은 snapshot을 안전하게 정리합니다."""
    warnings: list[str] = []
    for snapshot in tuple(session.viewport_snapshots.values()):
        if not _restore_viewport_snapshot(snapshot):
            warnings.append("Viewport snapshot을 복원하지 못해 현재 값을 보존했습니다.")
    session.viewport_snapshots.clear()
    render_restored, camera_restored = _restore_render_if_unchanged(session)
    if not render_restored:
        warnings.append("Render의 사용자 변경을 보존했습니다.")
    if not camera_restored:
        warnings.append("Scene Camera의 사용자 변경을 보존했습니다.")
    try:
        session.scene.colmap_import.review_restore_available = False
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    return tuple(warnings)


__all__ = [
    "look_through_current",
    "restore_previous_view",
    "restore_session_views",
    "update_review_view_for_current",
]
