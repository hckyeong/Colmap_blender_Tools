"""Blender Camera Runtime을 KDB-tree에 연결하고 현재 시점 후보를 평가합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from bpy_extras import view3d_utils
from mathutils import Vector

from ..core.kdb_tree import KDBSourcePoint, KDBTree, build_kdb_tree, find_nearest
from .camera_scene import CameraManifestEntry, CameraRuntime


MAX_NEAREST_CANDIDATES = 8
DISTANCE_WEIGHT = 0.65
DIRECTION_WEIGHT = 0.35
SCORE_EPSILON = 1.0e-9


class CameraSpatialError(RuntimeError):
    """Camera KDB Index 또는 Viewport 시점 계약 위반을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class ViewportPose:
    """Camera 검색 직전의 World 시점 위치와 정규화된 방향을 보관합니다."""

    origin: tuple[float, float, float]
    direction: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ViewportNavigationState:
    """실패 시 되돌릴 호출 Viewport의 Camera와 탐색 상태를 보관합니다."""

    space_camera: Any
    perspective: str
    view_location: tuple[float, float, float]
    view_rotation: tuple[float, float, float, float]
    view_distance: float
    view_camera_offset: tuple[float, float]
    view_camera_zoom: float


@dataclass(frozen=True, slots=True)
class CameraCandidate:
    """한 Camera 후보의 거리와 방향 일치 및 종합 점수를 보관합니다."""

    entry: CameraManifestEntry
    distance: float
    alignment: float
    score: float


@dataclass(slots=True)
class CameraKDBIndex:
    """한 Camera Runtime에 대응하는 재생성 가능한 KDB 공간 인덱스입니다."""

    dataset_uuid: str
    dataset_revision: int
    axis_root_pointer: int
    axis_root_matrix_signature: tuple[float, ...]
    entries: tuple[CameraManifestEntry, ...]
    camera_pointers: tuple[int, ...]
    directions: tuple[tuple[float, float, float], ...]
    dataset_epsilon: float
    tree: KDBTree


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def _vector_tuple(value: Any) -> tuple[float, float, float]:
    """3차원 벡터를 유한한 float 튜플로 검증해 반환합니다."""

    result = tuple(float(component) for component in value)
    if len(result) != 3 or not all(isfinite(component) for component in result):
        raise CameraSpatialError("Camera 공간 좌표에 유한하지 않은 값이 있습니다.")
    return result


def _normalized_tuple(value: Any, label: str) -> tuple[float, float, float]:
    """입력 벡터를 정규화하고 0 길이 또는 비유한 값을 거부합니다."""

    vector = Vector(_vector_tuple(value))
    if vector.length_squared <= 1.0e-20:
        raise CameraSpatialError(f"{label} 방향 벡터의 길이가 0입니다.")
    vector.normalize()
    return _vector_tuple(vector)


def _matrix_signature(matrix: Any) -> tuple[float, ...]:
    """World Matrix를 인덱스 유효성 비교용 16개 float 튜플로 변환합니다."""

    signature = tuple(
        float(matrix[row][column]) for row in range(4) for column in range(4)
    )
    if not all(isfinite(value) for value in signature):
        raise CameraSpatialError("Camera Axis Root 행렬에 유한하지 않은 값이 있습니다.")
    return signature


def _runtime_objects(runtime: CameraRuntime) -> tuple[Any, ...]:
    """Manifest 순서대로 모든 유효한 Camera Object를 반환합니다."""

    if not runtime.manifest.entries:
        raise CameraSpatialError("관리 Camera가 없습니다.")
    uuids = tuple(entry.camera_uuid for entry in runtime.manifest.entries)
    if len(set(uuids)) != len(uuids):
        raise CameraSpatialError("Camera Manifest에 중복 Camera UUID가 있습니다.")
    objects = tuple(runtime.objects.get(owned_uuid) for owned_uuid in uuids)
    for entry, camera in zip(runtime.manifest.entries, objects):
        if camera is None or not _pointer(camera) or getattr(camera, "type", "") != "CAMERA":
            raise CameraSpatialError(
                f"관리 Camera Object가 없습니다: IMAGE_ID {entry.image_id}"
            )
    return objects


def _axis_root(objects: tuple[Any, ...]) -> Any:
    """모든 관리 Camera가 공유하는 유효한 Axis Root를 반환합니다."""

    root = getattr(objects[0], "parent", None)
    if root is None or not _pointer(root):
        raise CameraSpatialError("Camera Axis Root가 없습니다.")
    if any(getattr(camera, "parent", None) is not root for camera in objects):
        raise CameraSpatialError("관리 Camera의 Axis Root가 서로 다릅니다.")
    return root


def _dataset_epsilon(positions: tuple[tuple[float, float, float], ...]) -> float:
    """Camera Bounding Box 크기에 비례하는 거리 Gate 최소 폭을 계산합니다."""

    minimum = tuple(min(position[axis] for position in positions) for axis in range(3))
    maximum = tuple(max(position[axis] for position in positions) for axis in range(3))
    return max(float((Vector(maximum) - Vector(minimum)).length) * 1.0e-6, 1.0e-6)


def build_camera_kdb_index(runtime: CameraRuntime) -> CameraKDBIndex:
    """현재 Camera Runtime의 World 위치와 촬영 방향으로 KDB Index를 구축합니다."""

    objects = _runtime_objects(runtime)
    root = _axis_root(objects)
    positions = tuple(_vector_tuple(camera.matrix_world.translation) for camera in objects)
    directions = tuple(
        _normalized_tuple(
            camera.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0)),
            "Camera",
        )
        for camera in objects
    )
    tree = build_kdb_tree(
        KDBSourcePoint(position, index, int(entry.image_id))
        for index, (position, entry) in enumerate(zip(positions, runtime.manifest.entries))
    )
    return CameraKDBIndex(
        dataset_uuid=str(runtime.manifest.dataset_uuid),
        dataset_revision=int(runtime.manifest.dataset_revision),
        axis_root_pointer=_pointer(root),
        axis_root_matrix_signature=_matrix_signature(root.matrix_world),
        entries=tuple(runtime.manifest.entries),
        camera_pointers=tuple(_pointer(camera) for camera in objects),
        directions=directions,
        dataset_epsilon=_dataset_epsilon(positions),
        tree=tree,
    )


def camera_kdb_index_is_current(index: CameraKDBIndex, runtime: CameraRuntime) -> bool:
    """Dataset·Axis Root·Camera 포인터가 구축 시점과 같은지 확인합니다."""

    try:
        objects = _runtime_objects(runtime)
        root = _axis_root(objects)
        return bool(
            index.dataset_uuid == str(runtime.manifest.dataset_uuid)
            and index.dataset_revision == int(runtime.manifest.dataset_revision)
            and index.entries == tuple(runtime.manifest.entries)
            and index.axis_root_pointer == _pointer(root)
            and index.axis_root_matrix_signature == _matrix_signature(root.matrix_world)
            and index.camera_pointers == tuple(_pointer(camera) for camera in objects)
        )
    except CameraSpatialError:
        return False


def _viewport_parts(context: Any) -> tuple[Any, Any, Any, Any]:
    """호출 Context에서 3D Viewport와 Window Region 및 RegionView3D를 반환합니다."""

    area = getattr(context, "area", None)
    space = getattr(context, "space_data", None)
    rv3d = getattr(space, "region_3d", None)
    if (
        area is None
        or getattr(area, "type", "") != "VIEW_3D"
        or space is None
        or getattr(space, "type", "") != "VIEW_3D"
        or rv3d is None
    ):
        raise CameraSpatialError("현재 영역이 3D Viewport가 아닙니다.")
    region = next(
        (candidate for candidate in area.regions if candidate.type == "WINDOW"),
        None,
    )
    if region is None or int(region.width) <= 0 or int(region.height) <= 0:
        raise CameraSpatialError("3D Viewport의 Window Region을 찾을 수 없습니다.")
    return area, region, space, rv3d


def viewport_pose(context: Any) -> ViewportPose:
    """버튼을 누른 3D Viewport의 변경 전 World 시점 위치와 방향을 캡처합니다."""

    _area, region, space, rv3d = _viewport_parts(context)
    perspective = str(rv3d.view_perspective)
    if perspective == "ORTHO":
        raise CameraSpatialError(
            "Orthographic View에서는 사용할 수 없습니다. Perspective View로 전환하십시오."
        )
    if perspective == "CAMERA":
        camera = getattr(space, "camera", None) or getattr(context.scene, "camera", None)
        if camera is None or not _pointer(camera) or getattr(camera, "type", "") != "CAMERA":
            raise CameraSpatialError("현재 Camera View의 Camera를 찾을 수 없습니다.")
        return ViewportPose(
            origin=_vector_tuple(camera.matrix_world.translation),
            direction=_normalized_tuple(
                camera.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0)),
                "Viewport",
            ),
        )
    center = (float(region.width) * 0.5, float(region.height) * 0.5)
    return ViewportPose(
        origin=_vector_tuple(
            view3d_utils.region_2d_to_origin_3d(region, rv3d, center)
        ),
        direction=_normalized_tuple(
            view3d_utils.region_2d_to_vector_3d(region, rv3d, center),
            "Viewport",
        ),
    )


def capture_viewport_navigation(context: Any) -> ViewportNavigationState:
    """호출 Viewport의 변경 가능한 탐색 상태를 Rollback용으로 캡처합니다."""

    _area, _region, space, rv3d = _viewport_parts(context)
    return ViewportNavigationState(
        space_camera=getattr(space, "camera", None),
        perspective=str(rv3d.view_perspective),
        view_location=tuple(float(value) for value in rv3d.view_location),
        view_rotation=tuple(float(value) for value in rv3d.view_rotation),
        view_distance=float(rv3d.view_distance),
        view_camera_offset=tuple(float(value) for value in rv3d.view_camera_offset),
        view_camera_zoom=float(rv3d.view_camera_zoom),
    )


def restore_viewport_navigation(context: Any, state: ViewportNavigationState) -> None:
    """호출 Viewport를 캡처한 Camera와 탐색 상태로 되돌립니다."""

    area, _region, space, rv3d = _viewport_parts(context)
    space.camera = state.space_camera
    rv3d.view_location = state.view_location
    rv3d.view_rotation = state.view_rotation
    rv3d.view_distance = state.view_distance
    rv3d.view_camera_offset = state.view_camera_offset
    rv3d.view_camera_zoom = state.view_camera_zoom
    rv3d.view_perspective = state.perspective
    area.tag_redraw()


def nearest_camera_candidates(
    index: CameraKDBIndex,
    pose: ViewportPose,
    count: int = MAX_NEAREST_CANDIDATES,
) -> tuple[tuple[int, float], ...]:
    """Viewport 원점에서 가까운 Camera Entry Index와 거리를 반환합니다."""

    return tuple(
        (result.record_index, result.distance)
        for result in find_nearest(index.tree, pose.origin, count)
    )


def _alignment(view_direction: Vector, camera_direction: tuple[float, float, float]) -> float:
    """현재 시선과 Camera 촬영 방향의 내적을 -1부터 1로 제한합니다."""

    return max(-1.0, min(1.0, float(view_direction.dot(Vector(camera_direction)))))


def _candidate_is_better(candidate: CameraCandidate, best: CameraCandidate) -> bool:
    """점수 허용 오차 안에서 거리·방향·IMAGE_ID 순으로 후보를 비교합니다."""

    if candidate.score < best.score - SCORE_EPSILON:
        return True
    if abs(candidate.score - best.score) > SCORE_EPSILON:
        return False
    if candidate.distance < best.distance - SCORE_EPSILON:
        return True
    if abs(candidate.distance - best.distance) > SCORE_EPSILON:
        return False
    if candidate.alignment > best.alignment + SCORE_EPSILON:
        return True
    if abs(candidate.alignment - best.alignment) > SCORE_EPSILON:
        return False
    return int(candidate.entry.image_id) < int(best.entry.image_id)


def select_best_camera(index: CameraKDBIndex, pose: ViewportPose) -> CameraCandidate:
    """거리 Gate 안의 후보를 위치와 시선 방향 점수로 평가해 하나를 선택합니다."""

    direction = Vector(_normalized_tuple(pose.direction, "Viewport"))
    nearest = nearest_camera_candidates(index, pose)
    if not nearest:
        raise CameraSpatialError("KDB-tree에서 Camera 후보를 찾지 못했습니다.")
    nearest_distance = min(distance for _entry_index, distance in nearest)
    distance_limit = max(
        nearest_distance * 2.0,
        nearest_distance + float(index.dataset_epsilon),
    )
    denominator = max(distance_limit, float(index.dataset_epsilon))
    candidates: list[CameraCandidate] = []
    for entry_index, distance in nearest:
        if distance > distance_limit + SCORE_EPSILON:
            continue
        alignment = _alignment(direction, index.directions[entry_index])
        score = (
            DISTANCE_WEIGHT * min(1.0, distance / denominator)
            + DIRECTION_WEIGHT * (1.0 - alignment) * 0.5
        )
        candidates.append(
            CameraCandidate(
                entry=index.entries[entry_index],
                distance=distance,
                alignment=alignment,
                score=score,
            )
        )
    if not candidates:
        raise CameraSpatialError("거리 Gate 안에서 Camera 후보를 찾지 못했습니다.")
    best = candidates[0]
    for candidate in candidates[1:]:
        if _candidate_is_better(candidate, best):
            best = candidate
    return best


__all__ = [
    "CameraCandidate",
    "CameraKDBIndex",
    "CameraSpatialError",
    "ViewportNavigationState",
    "ViewportPose",
    "build_camera_kdb_index",
    "camera_kdb_index_is_current",
    "capture_viewport_navigation",
    "nearest_camera_candidates",
    "restore_viewport_navigation",
    "select_best_camera",
    "viewport_pose",
]
