"""관리 Point Mesh와 Camera 선택을 권위 Runtime 모델에 대해 검증합니다."""

from __future__ import annotations

import math
from dataclasses import dataclass

import bmesh
import bpy

from . import managed_scene as _managed_scene
from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY


class PointMeshIntegrityError(ValueError):
    """표시 Mesh가 권위 Reconstruction과 달라졌을 때 발생합니다."""


@dataclass(frozen=True)
class SelectionSnapshot:
    dataset_uuid: str
    scene_pointer: int
    point_object_pointer: int
    point_mesh_pointer: int
    selected_point3d_ids: tuple[int, ...]
    observing_image_ids: tuple[int, ...]
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    history_hash: str


_SNAPSHOTS: dict[str, SelectionSnapshot] = {}
_COORDINATE_REL_TOLERANCE = 1.0e-6
_COORDINATE_ABS_TOLERANCE = 1.0e-5


def clear_snapshots() -> None:
    _SNAPSHOTS.clear()


def clear_snapshot(dataset_uuid: str) -> None:
    _SNAPSHOTS.pop(dataset_uuid, None)


def set_snapshot(snapshot: SelectionSnapshot) -> None:
    _SNAPSHOTS[snapshot.dataset_uuid] = snapshot


def managed_point_object(context, dataset_uuid: str) -> bpy.types.Object:
    """기존 선택 모듈 import 경로를 유지하는 관리 Point 조회 wrapper입니다."""
    return _managed_scene.require_managed_point_object(context, dataset_uuid)


def lock_managed_point_objects(context, dataset_uuid: str | None = None) -> None:
    """기존 선택 모듈 import 경로를 유지하는 Point 잠금 wrapper입니다."""
    _managed_scene.lock_managed_point_objects(context, dataset_uuid)


def lock_managed_camera_objects(context, dataset_uuid: str | None = None) -> None:
    """기존 선택 모듈 import 경로를 유지하는 Camera 잠금 wrapper입니다."""
    _managed_scene.lock_managed_camera_objects(context, dataset_uuid)


def lock_managed_selection_objects(context, dataset_uuid: str | None = None) -> None:
    """기존 선택 모듈 import 경로를 유지하는 통합 잠금 wrapper입니다."""
    _managed_scene.lock_managed_selection_objects(context, dataset_uuid)


def valid_snapshot(context, state) -> SelectionSnapshot | None:
    snapshot = _SNAPSHOTS.get(state.dataset_uuid)
    if snapshot is None:
        return None
    if (
        snapshot.scene_pointer != context.scene.as_pointer()
        or snapshot.source_fingerprint != state.source.source_fingerprint
        or snapshot.edit_revision != state.edit_revision
        or snapshot.index_revision != state.index_revision
        or snapshot.history_hash != state.history_hash
    ):
        _SNAPSHOTS.pop(state.dataset_uuid, None)
        return None
    try:
        point_object = managed_point_object(context, state.dataset_uuid)
    except ValueError:
        _SNAPSHOTS.pop(state.dataset_uuid, None)
        return None
    if (
        point_object.as_pointer() != snapshot.point_object_pointer
        or point_object.data.as_pointer() != snapshot.point_mesh_pointer
        or not set(snapshot.selected_point3d_ids).issubset(
            state.current_reconstruction.points3d
        )
    ):
        _SNAPSHOTS.pop(state.dataset_uuid, None)
        return None
    return snapshot


def observing_images_for_points(state, point_ids: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                image_id
                for point_id in point_ids
                for image_id in state.indexes.point_to_images[point_id]
            }
        )
    )


def _coordinates_match(actual, expected: tuple[float, float, float]) -> bool:
    return all(
        math.isclose(
            float(actual[index]),
            expected[index],
            rel_tol=_COORDINATE_REL_TOLERANCE,
            abs_tol=_COORDINATE_ABS_TOLERANCE,
        )
        for index in range(3)
    )


def _validated_point_id(
    state,
    expected_ids: tuple[int, ...],
    seen_indexes: set[int],
    *,
    source_index: int,
    low: int,
    high: int,
    coordinates,
) -> int:
    if (
        source_index in seen_indexes
        or source_index < 0
        or source_index >= len(expected_ids)
    ):
        raise PointMeshIntegrityError(
            "Point Mesh의 source index 매핑이 손상되었습니다."
        )
    seen_indexes.add(source_index)
    point_id = ((high & 0xFFFFFFFF) << 32) | (low & 0xFFFFFFFF)
    expected_id = expected_ids[source_index]
    if expected_id != point_id:
        raise PointMeshIntegrityError(
            "Point Mesh의 POINT3D_ID 매핑이 현재 모델과 일치하지 않습니다."
        )
    if not _coordinates_match(
        coordinates,
        state.current_reconstruction.points3d[expected_id].xyz,
    ):
        raise PointMeshIntegrityError(
            f"POINT3D_ID {expected_id}의 표시 좌표가 권위 모델과 다릅니다."
        )
    return point_id


def collect_selected_point_ids(
    context,
    state,
    *,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    """Edit Mesh 전체를 한 번 검증하고 선택된 영속 Point3D ID를 반환합니다."""
    obj = managed_point_object(context, state.dataset_uuid)
    if context.active_object is not obj or obj.mode != "EDIT":
        raise ValueError("Edit Point Mode에서 Point3D를 선택하십시오.")

    mesh = obj.data
    bm = bmesh.from_edit_mesh(mesh)
    source_layer = bm.verts.layers.int.get("colmap_source_index")
    low_layer = bm.verts.layers.int.get("colmap_point3d_id_low")
    high_layer = bm.verts.layers.int.get("colmap_point3d_id_high")
    if source_layer is None or low_layer is None or high_layer is None:
        raise PointMeshIntegrityError(
            "Point Mesh의 COLMAP ID 속성이 누락되었습니다. 권위 모델로 복원합니다."
        )

    expected_ids = state.point_id_order
    if len(bm.verts) != len(expected_ids):
        raise PointMeshIntegrityError(
            "Point Mesh topology가 변경되었습니다. 일반 정점 추가·삭제·병합은 "
            "지원하지 않으므로 권위 모델로 복원합니다."
        )

    seen_indexes: set[int] = set()
    selected: set[int] = set()
    for vertex in bm.verts:
        point_id = _validated_point_id(
            state,
            expected_ids,
            seen_indexes,
            source_index=int(vertex[source_layer]),
            low=int(vertex[low_layer]),
            high=int(vertex[high_layer]),
            coordinates=vertex.co,
        )
        if vertex.select:
            selected.add(point_id)

    if len(seen_indexes) != len(expected_ids):
        raise PointMeshIntegrityError("Point Mesh의 source index가 완전하지 않습니다.")
    if not selected and not allow_empty:
        raise ValueError("Point3D를 하나 이상 선택하십시오.")
    return tuple(sorted(selected))


def collect_selected_point_ids_from_object_mesh(
    context,
    state,
    *,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    """외부 Edit Mode 종료 뒤 Object Mesh를 한 번 검증하고 선택 ID를 반환합니다."""
    obj = managed_point_object(context, state.dataset_uuid)
    if obj.mode != "OBJECT":
        raise ValueError("Point Object가 Object Mode로 전환되지 않았습니다.")

    mesh = obj.data
    attributes = {}
    for name in (
        "colmap_source_index",
        "colmap_point3d_id_low",
        "colmap_point3d_id_high",
    ):
        attribute = mesh.attributes.get(name)
        if (
            attribute is None
            or attribute.domain != "POINT"
            or attribute.data_type != "INT"
        ):
            raise PointMeshIntegrityError(
                "Point Mesh의 COLMAP ID 속성이 누락되거나 형식이 변경되었습니다. "
                "권위 모델로 복원합니다."
            )
        attributes[name] = attribute

    expected_ids = state.point_id_order
    if len(mesh.vertices) != len(expected_ids):
        raise PointMeshIntegrityError(
            "Point Mesh topology가 변경되었습니다. 일반 정점 추가·삭제·병합은 "
            "지원하지 않으므로 권위 모델로 복원합니다."
        )
    if any(len(attribute.data) != len(mesh.vertices) for attribute in attributes.values()):
        raise PointMeshIntegrityError("Point Mesh의 COLMAP ID 속성 길이가 손상되었습니다.")

    seen_indexes: set[int] = set()
    selected: set[int] = set()
    source_data = attributes["colmap_source_index"].data
    low_data = attributes["colmap_point3d_id_low"].data
    high_data = attributes["colmap_point3d_id_high"].data
    for vertex in mesh.vertices:
        index = vertex.index
        point_id = _validated_point_id(
            state,
            expected_ids,
            seen_indexes,
            source_index=int(source_data[index].value),
            low=int(low_data[index].value),
            high=int(high_data[index].value),
            coordinates=vertex.co,
        )
        if vertex.select:
            selected.add(point_id)

    if len(seen_indexes) != len(expected_ids):
        raise PointMeshIntegrityError("Point Mesh의 source index가 완전하지 않습니다.")
    if not selected and not allow_empty:
        raise ValueError("Point3D를 하나 이상 선택하십시오.")
    return tuple(sorted(selected))


def managed_camera_map(context, dataset_uuid: str) -> dict[int, bpy.types.Object]:
    """기존 선택 모듈 import 경로를 유지하는 Camera map wrapper입니다."""
    return _managed_scene.managed_camera_map(context, dataset_uuid)


def validate_managed_camera_scene(context, state) -> dict[int, bpy.types.Object]:
    """현재 Scene의 관리 Camera 집합이 COLMAP Image 집합과 같은지 확인합니다."""
    cameras = managed_camera_map(context, state.dataset_uuid)
    expected = set(state.current_reconstruction.images)
    actual = set(cameras)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            "Scene의 관리 Camera와 현재 COLMAP Image가 일치하지 않습니다. "
            f"누락={missing[:10]}, 예상 밖={extra[:10]}. 다시 Import하십시오."
        )
    return cameras


def collect_selected_image_ids(
    context,
    state,
    *,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    """Object Mode의 선택이 현재 데이터셋 Camera만 포함하는지 검증합니다."""
    if context.mode != "OBJECT":
        raise ValueError("Camera 제거는 Object Mode에서 실행하십시오.")
    selected = tuple(context.selected_objects)
    if not selected and not allow_empty:
        raise ValueError("제거할 COLMAP Camera를 하나 이상 선택하십시오.")
    image_ids: set[int] = set()
    for obj in selected:
        if (
            obj.get(OWNER_KEY) != OWNER_TOKEN
            or obj.get(RECORD_KIND_KEY) != "CAMERA"
            or obj.get(DATASET_UUID_KEY) != state.dataset_uuid
        ):
            raise ValueError(
                "선택에 현재 데이터셋의 관리 Camera가 아닌 Object가 섞여 있습니다."
            )
        try:
            image_id = int(obj.get("colmap_image_id_text"))
        except (TypeError, ValueError) as exc:
            raise ValueError("선택 Camera의 IMAGE_ID가 손상되었습니다.") from exc
        if image_id in image_ids:
            raise ValueError(f"선택 Camera의 IMAGE_ID {image_id}가 중복되었습니다.")
        if image_id not in state.current_reconstruction.images:
            raise ValueError(f"현재 모델에 없는 IMAGE_ID {image_id}가 선택되었습니다.")
        image_ids.add(image_id)
    return tuple(sorted(image_ids))
