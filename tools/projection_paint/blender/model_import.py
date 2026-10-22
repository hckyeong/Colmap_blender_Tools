"""공통 OBJ 가져오기와 Painter 전용 메시·축의 원자적 확정을 연결합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import bpy
from mathutils import Matrix

try:
    from ....shared.blender_runtime.obj_import import import_obj_to_staging
except ImportError:
    from shared.blender_runtime.obj_import import import_obj_to_staging

from .axis_roots import axis_matrix, axis_signature, create_axis_root
from .ownership import (
    MODEL_AXIS_REVISION_KEY,
    MODEL_AXIS_SIGNATURE_KEY,
    is_owned,
    tag_owned,
)
from .transactions import TransactionJournal


class ModelAxisError(RuntimeError):
    """Model Axis 적용 계약 위반을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class ModelAxisResult:
    """Model Axis 적용 결과와 중복 실행 여부를 반환합니다."""

    root: Any | None
    applied_count: int
    no_op: bool


def _obj_file_identity(path: Path) -> tuple[int, int, int, int]:
    """가져오기 전후의 크기·수정·변경 시간과 파일 식별자를 비교합니다."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def model_import_available(context: Any) -> bool:
    """페인팅이나 다른 입력 작업이 없는 로컬 장면에서만 OBJ 입력을 허용합니다."""
    from .import_progress_session import active_import_progress_session

    settings = getattr(getattr(context, "scene", None), "projection_paint", None)
    return bool(
        settings is not None
        and not settings.paint_enabled
        and str(settings.paint_state) not in {"ENTERING", "EXITING", "ACTIVE"}
        and active_import_progress_session() is None
        and context.scene.library is None
    )


def import_obj_model(context: Any, filepath: str | Path) -> Any:
    """원본 OBJ를 새 Painter 메시로 확정하고 기존 페인트 대상·레이어를 보존합니다."""
    if not model_import_available(context):
        raise ModelAxisError("페인팅과 진행 중인 가져오기를 종료한 뒤 OBJ를 불러오세요.")
    path = Path(bpy.path.abspath(str(filepath))).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".obj":
        raise ModelAxisError("가져올 OBJ 파일을 선택하세요.")
    source_identity = _obj_file_identity(path)
    settings = context.scene.projection_paint
    axis_matrix(settings.model_axis_preset, custom_forward=settings.model_forward, custom_up=settings.model_up)
    asset_uuid = uuid4().hex
    staging = import_obj_to_staging(path)
    collection = root = obj = None
    try:
        if source_identity != _obj_file_identity(path):
            raise ModelAxisError("가져오는 동안 OBJ 파일이 변경되었습니다. 다시 불러오세요.")
        collection = bpy.data.collections.new(f"PP_MODEL_{asset_uuid[:8]}")
        tag_owned(collection, "MODEL_COLLECTION", dataset_uuid=asset_uuid)
        root = create_axis_root(
            f"PP_MODEL_AXIS_{asset_uuid[:8]}",
            kind="MODEL_AXIS_ROOT",
            dataset_uuid=asset_uuid,
            preset=settings.model_axis_preset,
            custom_forward=settings.model_forward,
            custom_up=settings.model_up,
        )
        collection.objects.link(root)
        obj = bpy.data.objects.new(path.stem, staging.mesh)
        tag_owned(obj, "MODEL_OBJECT", dataset_uuid=asset_uuid)
        tag_owned(staging.mesh, "MODEL_MESH", dataset_uuid=asset_uuid)
        obj["pp_source_path"] = str(path)
        obj.parent = root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = Matrix.Identity(4)
        obj[MODEL_AXIS_REVISION_KEY] = f"1:{axis_signature(settings.model_axis_preset, custom_forward=settings.model_forward, custom_up=settings.model_up)}"
        collection.objects.link(obj)
        for material in staging.materials:
            tag_owned(material, "MODEL_MATERIAL", dataset_uuid=asset_uuid)
        for image in staging.images:
            tag_owned(image, "MODEL_IMAGE", dataset_uuid=asset_uuid)
        context.scene.collection.children.link(collection)
        from .dataset_view_layer import synchronize_projection_dataset_visibility

        synchronize_projection_dataset_visibility(context.scene)
        staging.commit()
        return obj
    except Exception:
        for item in (obj, root):
            if item is not None and bpy.data.objects.get(item.name) == item:
                bpy.data.objects.remove(item, do_unlink=True)
        if collection is not None and bpy.data.collections.get(collection.name) == collection:
            bpy.data.collections.remove(collection, do_unlink=True)
        staging.discard()
        raise


class PP_OT_import_obj(bpy.types.Operator):
    """파일 선택창에서 OBJ를 골라 Painter 소유의 새 실제 메시로 가져옵니다."""

    bl_idname = "projection_paint.import_obj"
    bl_label = "OBJ 불러오기"
    bl_description = "기존 페인트 대상과 레이어를 유지하며 OBJ 메시를 추가합니다"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.obj", options={"HIDDEN"})

    @classmethod
    def poll(cls, context: Any) -> bool:
        """진행 중인 입력이나 페인팅이 없을 때만 버튼을 활성화합니다."""
        return model_import_available(context)

    def invoke(self, context: Any, event: Any) -> set[str]:
        """입력 설정을 변경하지 않고 OBJ 파일 선택창을 엽니다."""
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context: Any) -> set[str]:
        """선택 파일의 확정 결과나 실패 원인을 Painter 상태에 표시합니다."""
        settings = context.scene.projection_paint
        try:
            obj = import_obj_model(context, self.filepath)
        except (OSError, RuntimeError, ValueError) as exc:
            settings.status = f"OBJ 가져오기 실패: {exc}"
            settings.status_kind = "ERROR"
            self.report({"ERROR"}, settings.status)
            return {"CANCELLED"}
        settings.status = f"OBJ 가져오기 완료: {obj.name}. Paint Target에서 선택하세요."
        settings.status_kind = "SUCCESS"
        return {"FINISHED"}


def open_blender_import_menu(context: Any) -> set[str]:
    """현재 Window에서 Blender 기본 File Import 메뉴를 엽니다."""

    if getattr(context, "window", None) is None:
        raise ModelAxisError("Blender Import 메뉴를 열 Window가 없습니다.")
    return bpy.ops.wm.call_menu(name="TOPBAR_MT_file_import")


def _editable_meshes(objects: Iterable[Any]) -> tuple[Any, ...]:
    """선택 항목을 중복 없는 로컬 편집 가능 Mesh로 검증합니다."""

    result = []
    seen = set()
    for obj in objects:
        pointer = int(obj.as_pointer())
        if pointer in seen:
            continue
        seen.add(pointer)
        if obj.type != "MESH":
            continue
        if obj.library is not None or obj.override_library is not None:
            raise ModelAxisError(f"로컬 편집 불가능한 Mesh입니다: {obj.name}")
        if obj.data is None or obj.data.library is not None:
            raise ModelAxisError(f"로컬 편집 불가능한 Mesh Data입니다: {obj.name}")
        result.append(obj)
    if not result:
        raise ModelAxisError("축을 적용할 로컬 Mesh가 없습니다.")
    return tuple(result)


def _top_level_selection(objects: tuple[Any, ...]) -> tuple[Any, ...]:
    """선택 내부 계층에서 최상위 Object만 Root에 연결합니다."""

    selected = {int(obj.as_pointer()) for obj in objects}
    result = []
    for obj in objects:
        parent = obj.parent
        has_selected_parent = False
        while parent is not None:
            if int(parent.as_pointer()) in selected:
                has_selected_parent = True
                break
            parent = parent.parent
        if not has_selected_parent:
            result.append(obj)
    return tuple(result)


def _find_axis_root(scene: Any, signature: str, dataset_uuid: str) -> Any | None:
    """현재 Scene에서 같은 축 계약의 관리 Root를 찾습니다."""

    matches = [
        obj
        for obj in scene.objects
        if is_owned(obj, kind="MODEL_AXIS_ROOT", dataset_uuid=dataset_uuid)
        and obj.get(MODEL_AXIS_SIGNATURE_KEY) == signature
    ]
    if len(matches) > 1:
        raise ModelAxisError("동일한 Model Axis Root가 중복되어 있습니다.")
    return matches[0] if matches else None


def apply_model_axis(
    scene: Any,
    objects: Iterable[Any],
    *,
    preset: str,
    custom_forward: str = "-Z",
    custom_up: str = "Y",
    dataset_uuid: str = "",
    axis_revision: str = "1",
) -> ModelAxisResult:
    """선택 Mesh에 Model Axis를 정확히 한 번 원자적으로 적용합니다."""

    meshes = _editable_meshes(objects)
    signature = axis_signature(
        preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
    )
    revision = f"{axis_revision}:{signature}"
    existing = {str(obj.get(MODEL_AXIS_REVISION_KEY, "") or "") for obj in meshes}
    if existing == {revision}:
        root = _find_axis_root(scene, signature, dataset_uuid)
        return ModelAxisResult(root, 0, True)
    if any(existing):
        raise ModelAxisError("이미 다른 Model Axis가 적용된 Mesh가 포함되어 있습니다.")

    journal = TransactionJournal("Apply Model Axis")
    root = _find_axis_root(scene, signature, dataset_uuid)
    try:
        if root is None:
            root = create_axis_root(
                "PP_MODEL_AXIS_ROOT",
                kind="MODEL_AXIS_ROOT",
                dataset_uuid=dataset_uuid,
                preset=preset,
                custom_forward=custom_forward,
                custom_up=custom_up,
            )
            scene.collection.objects.link(root)

            def remove_root() -> None:
                """실패 시 새 Model Axis Root만 제거합니다."""

                if root is not None and root.name in bpy.data.objects:
                    bpy.data.objects.remove(root, do_unlink=True)

            journal.add_rollback("Model Axis Root 제거", remove_root)
        for obj in meshes:
            journal.record_mapping_value(obj, MODEL_AXIS_REVISION_KEY)
        for obj in _top_level_selection(meshes):
            old_parent = obj.parent
            old_parent_inverse = obj.matrix_parent_inverse.copy()
            old_world = obj.matrix_world.copy()

            def restore_object(
                target=obj,
                parent=old_parent,
                parent_inverse=old_parent_inverse,
                world=old_world,
            ) -> None:
                """실패 시 Object의 Parent와 World Transform을 복구합니다."""

                target.parent = parent
                target.matrix_parent_inverse = parent_inverse
                target.matrix_world = world

            journal.add_rollback(f"{obj.name} Parent 복구", restore_object)
            obj.parent = root
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = root.matrix_world @ old_world
        for obj in meshes:
            obj[MODEL_AXIS_REVISION_KEY] = revision
        journal.commit()
        return ModelAxisResult(root, len(meshes), False)
    except Exception:
        journal.abort()
        raise


class PP_OT_open_model_import(bpy.types.Operator):
    """Blender 기본 모델 Import 메뉴를 엽니다."""

    bl_idname = "projection_paint.open_model_import"
    bl_label = "Open Blender Import"

    def execute(self, context: Any) -> set[str]:
        """Blender 기본 Import 메뉴 호출 결과를 반환합니다."""

        try:
            return open_blender_import_menu(context)
        except (ModelAxisError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PP_OT_apply_model_axis(bpy.types.Operator):
    """현재 선택 Mesh에 독립 Model Axis 프리셋을 적용합니다."""

    bl_idname = "projection_paint.apply_model_axis"
    bl_label = "Apply Model Axis"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: Any) -> set[str]:
        """Scene 설정과 현재 선택을 사용해 Model Axis를 적용합니다."""

        settings = context.scene.projection_paint
        try:
            result = apply_model_axis(
                context.scene,
                context.selected_objects,
                preset=settings.model_axis_preset,
                custom_forward=settings.model_forward,
                custom_up=settings.model_up,
                dataset_uuid=settings.dataset_uuid,
                axis_revision=str(settings.dataset_revision or 1),
            )
        except (ModelAxisError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.status = (
            "Model Axis가 이미 적용되어 있습니다."
            if result.no_op
            else f"Mesh {result.applied_count}개에 Model Axis를 적용했습니다."
        )
        settings.status_kind = "SUCCESS"
        return {"FINISHED"}


CLASSES = (PP_OT_import_obj, PP_OT_apply_model_axis)
