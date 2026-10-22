"""Blender 5.2 OBJ Importer를 격리 호출해 원본 Mesh만 Staging에 남깁니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import bpy
from mathutils import Matrix


class ObjNativeImportError(RuntimeError):
    """Blender 기본 OBJ Importer 또는 Staging 정리 실패를 나타냅니다."""


@dataclass(slots=True)
class NativeObjStaging:
    """이번 native import가 생성해 실패·취소 때만 제거할 Data Block을 소유합니다."""

    mesh: Any
    materials: tuple[Any, ...] = ()
    images: tuple[Any, ...] = ()
    committed: bool = False
    _discarded: bool = field(default=False, repr=False)

    def commit(self) -> Any:
        """원본 메시 소유권을 호출자에게 넘기고 사용하지 않는 부속만 정리합니다."""

        if self._discarded:
            raise ObjNativeImportError("이미 폐기한 OBJ Staging을 확정할 수 없습니다.")
        self._remove_unused_support_data()
        self.committed = True
        return self.mesh

    def discard(self) -> None:
        """실패·취소 때 이번 Import가 만든 Mesh와 미사용 부속 Data Block만 제거합니다."""

        if self._discarded or self.committed:
            return
        mesh = self.mesh
        if _contains_id(bpy.data.meshes, mesh) and not any(
            getattr(obj, "data", None) == mesh for obj in bpy.data.objects
        ):
            try:
                self._remove_mesh(mesh)
            except (ReferenceError, RuntimeError, TypeError, ValueError) as error:
                self._handle_mesh_removal_failure(mesh, error)
        self.mesh = None
        self._discard_support_data()
        self._discarded = True

    def _remove_mesh(self, mesh: Any) -> None:
        """이번 가져오기의 분리된 메시를 제거합니다."""

        _remove_mesh_data_block(mesh)

    def _handle_mesh_removal_failure(self, mesh: Any, error: Exception) -> None:
        """메시 제거 실패를 호출자가 처리할 수 있도록 전달합니다."""

        raise ObjNativeImportError("OBJ Staging Mesh를 제거하지 못했습니다.") from error

    def _discard_support_data(self) -> None:
        """이번 가져오기가 생성한 미사용 부속 데이터만 제거합니다."""

        self._remove_unused_support_data()

    def _remove_unused_support_data(self) -> None:
        """이번 Import가 만들었지만 유지 Mesh에서 참조하지 않는 Material·Image만 제거합니다."""

        for material in self.materials:
            try:
                if _contains_id(bpy.data.materials, material) and material.users == 0:
                    bpy.data.materials.remove(material)
            except ReferenceError:
                continue
        for image in self.images:
            try:
                if _contains_id(bpy.data.images, image) and image.users == 0:
                    bpy.data.images.remove(image)
            except ReferenceError:
                continue


def _contains_id(collection: Any, item: Any) -> bool:
    """동명이인 데이터가 아닌 추적한 Blender ID 자체가 남아 있는지 확인합니다."""

    try:
        return item is not None and collection.get(item.name) == item
    except ReferenceError:
        return False


def _restore_context_state(
    view_layer: Any,
    selected_objects: tuple[Any, ...],
    active_object: Any | None,
    active_mode: str,
    active_layer_collection: Any | None,
) -> None:
    """Native Import 전 선택·활성 Object·모드·활성 Collection을 복원합니다."""

    for obj in tuple(getattr(view_layer, "objects", ())):
        if obj is None:
            continue
        try:
            obj.select_set(False)
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    for obj in selected_objects:
        if obj is None:
            continue
        try:
            if _contains_id(bpy.data.objects, obj):
                obj.select_set(True)
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    try:
        view_layer.objects.active = (
            active_object
            if active_object is not None and active_object.name in view_layer.objects
            else None
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        view_layer.objects.active = None
    if active_layer_collection is not None:
        try:
            view_layer.active_layer_collection = active_layer_collection
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    if active_object is not None and active_mode and active_mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode=active_mode)
        except (AttributeError, RuntimeError):
            pass


def _remove_objects(objects: tuple[Any, ...]) -> None:
    """이번 Import가 생성한 Object만 모든 Collection에서 제거합니다."""

    for obj in objects:
        if _contains_id(bpy.data.objects, obj):
            bpy.data.objects.remove(obj, do_unlink=True)


def _remove_mesh_data_block(mesh: Any) -> None:
    """Staging 폐기 테스트가 주입할 수 있는 단일 Blender Mesh 제거 경계를 제공합니다."""

    bpy.data.meshes.remove(mesh)


def _remove_empty_collections(collections: tuple[Any, ...]) -> None:
    """이번 Import가 생성하고 비어 있는 Collection만 제거합니다."""

    for collection in collections:
        if (
            _contains_id(bpy.data.collections, collection)
            and not collection.objects
            and not collection.children
        ):
            bpy.data.collections.remove(collection)


def import_obj_to_staging(path: str | Path) -> NativeObjStaging:
    """OBJ를 원본 로컬 좌표로 가져오고 Scene에는 Object를 남기지 않습니다."""

    source = Path(path).expanduser().resolve(strict=False)
    context = bpy.context
    view_layer = context.view_layer
    selected_before = tuple(context.selected_objects)
    active_before = view_layer.objects.active
    active_mode = str(getattr(active_before, "mode", "OBJECT"))
    active_layer_collection_before = view_layer.active_layer_collection
    objects_before = set(bpy.data.objects)
    meshes_before = set(bpy.data.meshes)
    materials_before = set(bpy.data.materials)
    images_before = set(bpy.data.images)
    collections_before = set(bpy.data.collections)
    created_objects: tuple[Any, ...] = ()
    created_meshes: tuple[Any, ...] = ()
    created_materials: tuple[Any, ...] = ()
    created_images: tuple[Any, ...] = ()
    created_collections: tuple[Any, ...] = ()
    try:
        if active_before is not None and active_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        result = bpy.ops.wm.obj_import(
            filepath=str(source),
            global_scale=1.0,
            clamp_size=0.0,
            forward_axis="Y",
            up_axis="Z",
            use_split_objects=False,
            use_split_groups=False,
            import_vertex_groups=False,
            validate_meshes=True,
            close_spline_loops=True,
            collection_separator="",
            mtl_name_collision_mode="MAKE_UNIQUE",
        )
        if "FINISHED" not in result:
            raise ObjNativeImportError("Blender OBJ Importer가 완료 상태를 반환하지 않았습니다.")
        created_objects = tuple(obj for obj in bpy.data.objects if obj not in objects_before)
        created_meshes = tuple(mesh for mesh in bpy.data.meshes if mesh not in meshes_before)
        created_materials = tuple(item for item in bpy.data.materials if item not in materials_before)
        created_images = tuple(item for item in bpy.data.images if item not in images_before)
        created_collections = tuple(item for item in bpy.data.collections if item not in collections_before)
        mesh_objects = tuple(obj for obj in created_objects if getattr(obj, "type", "") == "MESH")
        if len(mesh_objects) != 1 or len(created_meshes) != 1:
            raise ObjNativeImportError("OBJ 한 파일에서 단일 원본 Mesh를 만들지 못했습니다.")
        imported_object = mesh_objects[0]
        if imported_object.matrix_world != Matrix.Identity(4):
            raise ObjNativeImportError("OBJ Importer가 원본 좌표에 비Identity Object 변환을 추가했습니다.")
        mesh = imported_object.data
        if mesh not in created_meshes:
            raise ObjNativeImportError("OBJ Object와 새 Mesh Data Block 소유 관계가 올바르지 않습니다.")
        _remove_objects(created_objects)
        _remove_empty_collections(created_collections)
        if any(getattr(obj, "data", None) == mesh for obj in bpy.data.objects):
            raise ObjNativeImportError("OBJ 원본 Mesh를 Scene Object에서 분리하지 못했습니다.")
        return NativeObjStaging(mesh=mesh, materials=created_materials, images=created_images)
    except Exception as error:
        _remove_objects(tuple(obj for obj in bpy.data.objects if obj not in objects_before))
        _remove_empty_collections(tuple(item for item in bpy.data.collections if item not in collections_before))
        for mesh in tuple(item for item in bpy.data.meshes if item not in meshes_before):
            if not any(getattr(obj, "data", None) == mesh for obj in bpy.data.objects):
                mesh.use_fake_user = False
                bpy.data.meshes.remove(mesh)
        for material in tuple(item for item in bpy.data.materials if item not in materials_before):
            if material.users == 0:
                bpy.data.materials.remove(material)
        for image in tuple(item for item in bpy.data.images if item not in images_before):
            if image.users == 0:
                bpy.data.images.remove(image)
        if isinstance(error, ObjNativeImportError):
            raise
        raise ObjNativeImportError(f"Blender OBJ Importer 실행에 실패했습니다: {error}") from error
    finally:
        _restore_context_state(
            view_layer,
            selected_before,
            active_before,
            active_mode,
            active_layer_collection_before,
        )


__all__ = ("NativeObjStaging", "ObjNativeImportError", "import_obj_to_staging")
