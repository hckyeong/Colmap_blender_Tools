"""staging Scene의 commit, rollback과 애드온 소유 orphan 정리를 담당합니다."""

from __future__ import annotations

from dataclasses import dataclass, field

import bpy
from mathutils import Matrix

from ..core.import_types import DEFAULT_CAMERA_FRUSTUM_SIZE
from ..core.model import LoadedReconstruction
from .managed_scene import (
    collection_camera_map,
    find_collection_axis_root,
    iter_managed_roots,
    require_collection_point_object,
    require_managed_child_collection,
    require_point_display_modifier,
)
from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN
from .scene_elements import (
    copy_managed_metadata,
    create_staging_collection_tree,
    point_scale_socket_identifier,
    populate_staging_collection_tree,
    set_modifier_point_scale,
)


@dataclass
class StagedScene:
    """아직 Scene Root에 연결하지 않은 관리 Collection 트리입니다."""

    root: bpy.types.Collection


@dataclass
class _SameDatasetCommit:
    """같은 Dataset 인플레이스 갱신에 필요한 참조와 진행 상태입니다."""

    scene: bpy.types.Scene
    staged: StagedScene
    existing_root: bpy.types.Collection
    existing_points_collection: bpy.types.Collection
    staged_points_collection: bpy.types.Collection
    existing_cameras_collection: bpy.types.Collection
    staged_cameras_collection: bpy.types.Collection
    existing_guides_collection: bpy.types.Collection
    staged_guides_collection: bpy.types.Collection
    existing_point: bpy.types.Object
    staged_point: bpy.types.Object
    existing_modifier: bpy.types.NodesModifier
    staged_modifier: bpy.types.NodesModifier
    existing_cameras: dict[str, bpy.types.Object]
    staged_cameras: dict[str, bpy.types.Object]
    existing_axis_root: bpy.types.Object | None
    staged_axis_root: bpy.types.Object
    moved_new_cameras: list[bpy.types.Object] = field(default_factory=list)
    unlinked_removed_cameras: list[
        tuple[bpy.types.Object, tuple[bpy.types.Collection, ...]]
    ] = field(default_factory=list)
    moved_axis_root: bool = False


@dataclass(frozen=True)
class _CommitSnapshot:
    """예외 시 기존 Scene identity와 변경 가능한 상태를 모두 되돌립니다."""

    old_axis_state: tuple | None
    old_point_data: bpy.types.Mesh
    old_point_node_group: bpy.types.GeometryNodeTree
    old_point_scale: float
    old_point_transform: tuple
    old_point_properties: dict
    old_point_selection_state: tuple
    old_camera_state: dict[str, tuple]
    old_collection_state: tuple[tuple[bpy.types.Collection, dict], ...]


def _copy_custom_property_value(value):
    """롤백 뒤에도 유효하도록 Blender IDProperty 값을 순수 Python 값으로 복제합니다."""
    if hasattr(value, "to_list"):
        return [_copy_custom_property_value(item) for item in value.to_list()]
    if hasattr(value, "items"):
        return {
            str(key): _copy_custom_property_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_copy_custom_property_value(item) for item in value]
    return value


def _snapshot_custom_properties(data_block) -> dict:
    """Blender IDProperty 참조가 삭제로 무효화되지 않도록 값을 깊게 복제합니다."""
    return {
        str(key): _copy_custom_property_value(value)
        for key, value in data_block.items()
    }


def build_staging(
    loaded: LoadedReconstruction,
    point_size: float = 0.05,
    forward_axis: str = "Y",
    up_axis: str = "Z",
    camera_frustum_size: float = DEFAULT_CAMERA_FRUSTUM_SIZE,
) -> StagedScene:
    """검증된 Reconstruction으로 Scene에 연결하지 않은 staging을 만듭니다."""
    (
        root,
        points_collection,
        cameras_collection,
        guides_collection,
    ) = create_staging_collection_tree(loaded)
    staged = StagedScene(root)
    try:
        populate_staging_collection_tree(
            loaded,
            points_collection,
            cameras_collection,
            guides_collection,
            point_size=point_size,
            camera_frustum_size=camera_frustum_size,
            forward_axis=forward_axis,
            up_axis=up_axis,
        )
    except Exception:
        cleanup_staging(staged)
        raise
    return staged


def _lock_point_object_in_scene(
    scene: bpy.types.Scene,
    point_object: bpy.types.Object,
) -> None:
    """Scene commit 경계에서 관리 Point Object를 선택 불가 상태로 정규화합니다."""
    point_object.hide_select = True
    point_object.select_set(False)
    for view_layer in scene.view_layers:
        if view_layer.objects.active is point_object:
            view_layer.objects.active = None


def _lock_camera_object_in_scene(
    scene: bpy.types.Scene,
    camera_object: bpy.types.Object,
) -> None:
    """Scene commit 경계에서 관리 Camera를 선택 불가 상태로 정규화합니다."""
    camera_object.hide_select = True
    camera_object.select_set(False)
    for view_layer in scene.view_layers:
        if view_layer.objects.active is camera_object:
            view_layer.objects.active = None


def _resolve_same_dataset_commit(
    scene: bpy.types.Scene,
    existing_root: bpy.types.Collection,
    staged: StagedScene,
) -> _SameDatasetCommit:
    """인플레이스 갱신 대상이 정확한 관리 구조인지 한 번 검증합니다."""
    existing_points_collection = require_managed_child_collection(
        existing_root,
        "POINTS_COLLECTION",
    )
    staged_points_collection = require_managed_child_collection(
        staged.root,
        "POINTS_COLLECTION",
    )
    existing_cameras_collection = require_managed_child_collection(
        existing_root,
        "CAMERAS_COLLECTION",
    )
    staged_cameras_collection = require_managed_child_collection(
        staged.root,
        "CAMERAS_COLLECTION",
    )
    existing_guides_collection = require_managed_child_collection(
        existing_root,
        "GUIDES_COLLECTION",
    )
    staged_guides_collection = require_managed_child_collection(
        staged.root,
        "GUIDES_COLLECTION",
    )
    existing_point = require_collection_point_object(existing_points_collection)
    staged_point = require_collection_point_object(staged_points_collection)
    staged_axis_root = find_collection_axis_root(staged_guides_collection)
    if staged_axis_root is None:
        raise RuntimeError("staging에 표시 축 Root가 없습니다.")
    return _SameDatasetCommit(
        scene=scene,
        staged=staged,
        existing_root=existing_root,
        existing_points_collection=existing_points_collection,
        staged_points_collection=staged_points_collection,
        existing_cameras_collection=existing_cameras_collection,
        staged_cameras_collection=staged_cameras_collection,
        existing_guides_collection=existing_guides_collection,
        staged_guides_collection=staged_guides_collection,
        existing_point=existing_point,
        staged_point=staged_point,
        existing_modifier=require_point_display_modifier(existing_point),
        staged_modifier=require_point_display_modifier(staged_point),
        existing_cameras=collection_camera_map(existing_cameras_collection),
        staged_cameras=collection_camera_map(staged_cameras_collection),
        existing_axis_root=find_collection_axis_root(existing_guides_collection),
        staged_axis_root=staged_axis_root,
    )


def _capture_commit_snapshot(transaction: _SameDatasetCommit) -> _CommitSnapshot:
    """변경 전에 기존 데이터 연결과 변환을 rollback용으로 보존합니다."""
    existing_axis_root = transaction.existing_axis_root
    old_axis_state = (
        None
        if existing_axis_root is None
        else (
            existing_axis_root.matrix_world.copy(),
            _snapshot_custom_properties(existing_axis_root),
        )
    )
    existing_point = transaction.existing_point
    old_point_node_group = transaction.existing_modifier.node_group
    point_scale_identifier = point_scale_socket_identifier(old_point_node_group)
    point_scale_input = getattr(
        transaction.existing_modifier.properties.inputs,
        point_scale_identifier,
        None,
    )
    if point_scale_input is None:
        raise RuntimeError("기존 Point 표시 Modifier에 Point Scale 입력이 없습니다.")
    return _CommitSnapshot(
        old_axis_state=old_axis_state,
        old_point_data=existing_point.data,
        old_point_node_group=old_point_node_group,
        old_point_scale=point_scale_input.value,
        old_point_transform=(
            existing_point.parent,
            existing_point.matrix_parent_inverse.copy(),
            existing_point.matrix_basis.copy(),
        ),
        old_point_properties=_snapshot_custom_properties(existing_point),
        old_point_selection_state=(
            existing_point.hide_select,
            existing_point.select_get(),
            tuple(
                view_layer
                for view_layer in transaction.scene.view_layers
                if view_layer.objects.active is existing_point
            ),
        ),
        old_camera_state={
            image_id: (
                camera.data,
                camera.parent,
                camera.matrix_parent_inverse.copy(),
                camera.matrix_basis.copy(),
                _snapshot_custom_properties(camera),
                camera.hide_select,
                camera.select_get(),
                tuple(
                    view_layer
                    for view_layer in transaction.scene.view_layers
                    if view_layer.objects.active is camera
                ),
            )
            for image_id, camera in transaction.existing_cameras.items()
            if image_id in transaction.staged_cameras
        },
        old_collection_state=tuple(
            (collection, _snapshot_custom_properties(collection))
            for collection in (
                transaction.existing_root,
                transaction.existing_points_collection,
                transaction.existing_cameras_collection,
                transaction.existing_guides_collection,
            )
        ),
    )


def _apply_axis_update(transaction: _SameDatasetCommit) -> None:
    """기존 Axis identity를 유지하거나 staging Axis를 관리 트리로 이동합니다."""
    if transaction.existing_axis_root is None:
        transaction.staged_guides_collection.objects.unlink(
            transaction.staged_axis_root
        )
        try:
            transaction.existing_guides_collection.objects.link(
                transaction.staged_axis_root
            )
        except Exception:
            transaction.staged_guides_collection.objects.link(
                transaction.staged_axis_root
            )
            raise
        transaction.existing_axis_root = transaction.staged_axis_root
        transaction.moved_axis_root = True
        return
    existing_axis_root = transaction.existing_axis_root
    existing_axis_root.matrix_world = transaction.staged_axis_root.matrix_world
    copy_managed_metadata(transaction.staged_axis_root, existing_axis_root)
    existing_axis_root["colmap_display_forward_axis"] = transaction.staged_axis_root[
        "colmap_display_forward_axis"
    ]
    existing_axis_root["colmap_display_up_axis"] = transaction.staged_axis_root[
        "colmap_display_up_axis"
    ]


def _apply_point_update(transaction: _SameDatasetCommit) -> None:
    """기존 Point Object identity에 staging Mesh와 표시 Node Group을 연결합니다."""
    existing_point = transaction.existing_point
    staged_point = transaction.staged_point
    existing_point.data = staged_point.data
    transaction.existing_modifier.node_group = transaction.staged_modifier.node_group
    set_modifier_point_scale(
        transaction.existing_modifier,
        transaction.existing_modifier.node_group,
        staged_point["colmap_point_size"],
    )
    copy_managed_metadata(staged_point, existing_point)
    existing_point["colmap_point3d_id_encoding"] = staged_point[
        "colmap_point3d_id_encoding"
    ]
    existing_point["colmap_point_size"] = staged_point["colmap_point_size"]
    existing_point.parent = transaction.existing_axis_root
    existing_point.matrix_parent_inverse = Matrix.Identity(4)
    existing_point.matrix_basis = staged_point.matrix_basis
    _lock_point_object_in_scene(transaction.scene, existing_point)


def _apply_camera_updates(transaction: _SameDatasetCommit) -> None:
    """생존 Camera identity를 갱신하고 새 Camera만 staging에서 이동합니다."""
    for image_id, staged_camera in transaction.staged_cameras.items():
        existing_camera = transaction.existing_cameras.get(image_id)
        if existing_camera is None:
            transaction.staged_cameras_collection.objects.unlink(staged_camera)
            try:
                transaction.existing_cameras_collection.objects.link(staged_camera)
            except Exception:
                transaction.staged_cameras_collection.objects.link(staged_camera)
                raise
            transaction.moved_new_cameras.append(staged_camera)
            staged_camera.parent = transaction.existing_axis_root
            staged_camera.matrix_parent_inverse = Matrix.Identity(4)
            _lock_camera_object_in_scene(transaction.scene, staged_camera)
            continue
        existing_camera.data = staged_camera.data
        existing_camera.parent = transaction.existing_axis_root
        existing_camera.matrix_parent_inverse = Matrix.Identity(4)
        existing_camera.matrix_basis = staged_camera.matrix_basis
        copy_managed_metadata(staged_camera, existing_camera)
        existing_camera["colmap_image_id_text"] = staged_camera[
            "colmap_image_id_text"
        ]
        existing_camera["colmap_camera_id_text"] = staged_camera[
            "colmap_camera_id_text"
        ]
        existing_camera["colmap_image_name"] = staged_camera["colmap_image_name"]
        _lock_camera_object_in_scene(transaction.scene, existing_camera)


def _apply_collection_metadata(transaction: _SameDatasetCommit) -> None:
    """staging source 메타데이터를 기존 관리 Collection identity에 복사합니다."""
    copy_managed_metadata(transaction.staged.root, transaction.existing_root)
    copy_managed_metadata(
        transaction.staged_points_collection,
        transaction.existing_points_collection,
    )
    copy_managed_metadata(
        transaction.staged_cameras_collection,
        transaction.existing_cameras_collection,
    )
    copy_managed_metadata(
        transaction.staged_guides_collection,
        transaction.existing_guides_collection,
    )


def _unlink_removed_cameras(transaction: _SameDatasetCommit) -> None:
    """삭제 대상 Camera는 commit 중 복구할 수 있도록 연결만 해제합니다."""
    for image_id, camera in transaction.existing_cameras.items():
        if image_id in transaction.staged_cameras:
            continue
        linked_collections = tuple(camera.users_collection)
        transaction.unlinked_removed_cameras.append((camera, linked_collections))
        for collection in linked_collections:
            collection.objects.unlink(camera)


def _restore_custom_properties(data_block, values: dict) -> None:
    """기존 rollback과 같은 범위의 애드온 custom property를 복원합니다."""
    for key in list(data_block.keys()):
        if key.startswith("colmap_") or key.startswith("blender_colmap_"):
            del data_block[key]
    for key, value in values.items():
        data_block[key] = value


def _rollback_same_dataset(
    transaction: _SameDatasetCommit,
    snapshot: _CommitSnapshot,
) -> None:
    """기존 구현의 역순을 유지해 실패한 인플레이스 갱신을 되돌립니다."""
    existing_point = transaction.existing_point
    existing_point.data = snapshot.old_point_data
    transaction.existing_modifier.node_group = snapshot.old_point_node_group
    set_modifier_point_scale(
        transaction.existing_modifier,
        snapshot.old_point_node_group,
        snapshot.old_point_scale,
    )
    existing_point.parent = snapshot.old_point_transform[0]
    existing_point.matrix_parent_inverse = snapshot.old_point_transform[1]
    existing_point.matrix_basis = snapshot.old_point_transform[2]
    _restore_custom_properties(existing_point, snapshot.old_point_properties)
    existing_point.hide_select = False
    existing_point.select_set(snapshot.old_point_selection_state[1])
    existing_point.hide_select = snapshot.old_point_selection_state[0]
    for view_layer in snapshot.old_point_selection_state[2]:
        view_layer.objects.active = existing_point

    for image_id, state in snapshot.old_camera_state.items():
        camera = transaction.existing_cameras[image_id]
        camera.data = state[0]
        camera.parent = state[1]
        camera.matrix_parent_inverse = state[2]
        camera.matrix_basis = state[3]
        _restore_custom_properties(camera, state[4])
        camera.hide_select = False
        camera.select_set(state[6])
        camera.hide_select = state[5]
        for view_layer in state[7]:
            view_layer.objects.active = camera

    for camera in transaction.moved_new_cameras:
        transaction.existing_cameras_collection.objects.unlink(camera)
        transaction.staged_cameras_collection.objects.link(camera)
        camera.parent = transaction.staged_axis_root

    for camera, linked_collections in transaction.unlinked_removed_cameras:
        if camera.name not in bpy.data.objects:
            continue
        for collection in linked_collections:
            if (
                collection.name in bpy.data.collections
                and camera.name not in collection.objects
            ):
                collection.objects.link(camera)

    if transaction.moved_axis_root:
        transaction.existing_guides_collection.objects.unlink(
            transaction.staged_axis_root
        )
        transaction.staged_guides_collection.objects.link(
            transaction.staged_axis_root
        )
    elif snapshot.old_axis_state is not None:
        transaction.existing_axis_root.matrix_world = snapshot.old_axis_state[0]
        _restore_custom_properties(
            transaction.existing_axis_root,
            snapshot.old_axis_state[1],
        )

    for collection, properties in snapshot.old_collection_state:
        _restore_custom_properties(collection, properties)


def _finalize_same_dataset(transaction: _SameDatasetCommit) -> tuple[str, ...]:
    """성공한 commit은 유지하고 실패한 orphan 정리만 Warning으로 반환합니다."""
    warnings = []
    try:
        cleanup_staging(transaction.staged)
    except Exception as exc:
        warnings.append(f"staging datablock 정리에 실패했습니다: {exc}")
    for camera, _linked_collections in transaction.unlinked_removed_cameras:
        try:
            if camera.name in bpy.data.objects:
                bpy.data.objects.remove(camera, do_unlink=True)
        except Exception as exc:
            warnings.append(
                f"제거 Camera orphan datablock 정리에 실패했습니다: {camera.name}: {exc}"
            )
    try:
        _remove_owned_orphan_data_resources()
    except Exception as exc:
        warnings.append(f"미사용 관리 datablock 정리에 실패했습니다: {exc}")
    return tuple(warnings)


def _update_same_dataset_in_place(
    scene: bpy.types.Scene,
    existing_root: bpy.types.Collection,
    staged: StagedScene,
) -> tuple[str, ...]:
    """같은 Dataset의 관리 Object 정체성을 유지하며 staging 데이터로 갱신합니다."""
    transaction = _resolve_same_dataset_commit(scene, existing_root, staged)
    snapshot = _capture_commit_snapshot(transaction)
    try:
        _apply_axis_update(transaction)
        _apply_point_update(transaction)
        _apply_camera_updates(transaction)
        _apply_collection_metadata(transaction)
        _unlink_removed_cameras(transaction)
    except Exception:
        _rollback_same_dataset(transaction, snapshot)
        raise
    return _finalize_same_dataset(transaction)


def _remove_collection_tree(collection: bpy.types.Collection) -> None:
    for child in list(collection.children):
        if child.get(OWNER_KEY) == OWNER_TOKEN:
            _remove_collection_tree(child)
        else:
            collection.children.unlink(child)
    for obj in list(collection.objects):
        if obj.get(OWNER_KEY) != OWNER_TOKEN:
            collection.objects.unlink(obj)
            continue
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0 and data.get(OWNER_KEY) == OWNER_TOKEN:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
    bpy.data.collections.remove(collection)


def _remove_owned_orphan_point_resources() -> None:
    """이전 Import가 남긴 사용되지 않는 표시 노드와 머티리얼을 정리합니다."""
    for node_group in list(bpy.data.node_groups):
        if node_group.get(OWNER_KEY) == OWNER_TOKEN and node_group.users == 0:
            bpy.data.node_groups.remove(node_group)
    for material in list(bpy.data.materials):
        if material.get(OWNER_KEY) == OWNER_TOKEN and material.users == 0:
            bpy.data.materials.remove(material)


def _remove_owned_orphan_data_resources() -> None:
    """인플레이스 갱신 뒤 사용되지 않는 관리 데이터블록을 정리합니다."""
    for mesh in list(bpy.data.meshes):
        if mesh.get(OWNER_KEY) == OWNER_TOKEN and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    for camera in list(bpy.data.cameras):
        if camera.get(OWNER_KEY) == OWNER_TOKEN and camera.users == 0:
            bpy.data.cameras.remove(camera)
    _remove_owned_orphan_point_resources()


def _preserve_foreign_content(
    collection: bpy.types.Collection,
    destination: bpy.types.Collection,
) -> None:
    """관리 트리 안의 비소유 데이터를 Scene 루트로 옮겨 연결을 보존합니다."""
    for child in list(collection.children):
        if child.get(OWNER_KEY) == OWNER_TOKEN:
            _preserve_foreign_content(child, destination)
            continue
        if child.name not in destination.children:
            destination.children.link(child)
        collection.children.unlink(child)
    for obj in list(collection.objects):
        if obj.get(OWNER_KEY) == OWNER_TOKEN:
            continue
        if destination not in obj.users_collection:
            destination.objects.link(obj)
        collection.objects.unlink(obj)


def cleanup_staging(staged: StagedScene) -> None:
    """staging Collection 트리와 사용되지 않는 표시 리소스를 정리합니다."""
    if staged.root.name in bpy.data.collections:
        _remove_collection_tree(staged.root)
    _remove_owned_orphan_data_resources()


def commit_staging(scene: bpy.types.Scene, staged: StagedScene) -> tuple[str, ...]:
    """staging을 새 Dataset 교체 또는 같은 Dataset 인플레이스 방식으로 commit합니다."""
    previous_roots = list(iter_managed_roots(scene))
    same_dataset_roots = [
        root
        for root in previous_roots
        if root.get(DATASET_UUID_KEY) == staged.root.get(DATASET_UUID_KEY)
    ]
    if len(same_dataset_roots) == 1:
        return _update_same_dataset_in_place(scene, same_dataset_roots[0], staged)
    if len(same_dataset_roots) > 1:
        raise RuntimeError("같은 Dataset UUID를 가진 관리 Root가 중복되었습니다.")

    staged.root.name = staged.root.name.removesuffix("_STAGING")
    scene.collection.children.link(staged.root)
    staged_points_collection = require_managed_child_collection(
        staged.root,
        "POINTS_COLLECTION",
    )
    _lock_point_object_in_scene(
        scene,
        require_collection_point_object(staged_points_collection),
    )
    staged_cameras_collection = require_managed_child_collection(
        staged.root,
        "CAMERAS_COLLECTION",
    )
    for camera in collection_camera_map(staged_cameras_collection).values():
        _lock_camera_object_in_scene(scene, camera)
    warnings = []
    for previous in previous_roots:
        if previous != staged.root:
            try:
                _preserve_foreign_content(previous, scene.collection)
                scene.collection.children.unlink(previous)
                _remove_collection_tree(previous)
                _remove_owned_orphan_point_resources()
            except Exception as exc:
                # 새 장면 commit은 유지하고, 정리 실패한 이전 datablock만 orphan으로 남깁니다.
                warnings.append(f"이전 Import datablock 정리에 실패했습니다: {exc}")
    return tuple(warnings)
