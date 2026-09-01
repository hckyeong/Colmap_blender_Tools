"""Scene 공용 Dataset Snapshot, Revision과 Blender ID 조회를 관리합니다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import DATASET_SCHEMA_VERSION, DatasetImportMetadata, DatasetSnapshot
from .ownership import (
    RECORD_ID_KEY,
    SOURCE_FINGERPRINT_KEY,
    is_dataset_owned,
)


DisplayListener = Callable[[Any, DatasetSnapshot, frozenset[str]], None]
DISPLAY_FIELDS = frozenset(
    {
        "axis_preset",
        "axis_forward",
        "axis_up",
        "point_size",
        "camera_frustum_size",
    }
)
_DISPLAY_LISTENERS: dict[str, DisplayListener] = {}
_SUPPRESSED_SCENE_POINTERS: set[int] = set()


@dataclass(frozen=True, slots=True)
class DatasetResources:
    """공용 Dataset에 속한 Blender ID 한 벌을 읽기 전용으로 제공합니다."""

    root_collection: Any
    point_object: Any
    axis_root: Any
    camera_objects: Mapping[int, Any]


@dataclass
class DatasetMetadataTransaction:
    """Scene Property 변경을 Resource commit과 함께 되돌릴 수 있게 보존합니다."""

    scene: Any
    metadata: DatasetImportMetadata
    previous_values: dict[str, Any]
    next_values: dict[str, Any]
    changed_display_fields: frozenset[str]
    applied: bool = False
    completed: bool = False

    def apply(self) -> None:
        """검증된 다음 공용 Dataset 상태를 알림 없이 Scene에 기록합니다."""

        if self.applied:
            return
        _write_values(self.scene, self.next_values)
        self.applied = True

    def rollback(self) -> None:
        """완료 전 실패한 공용 Dataset 상태를 이전 Scene 값으로 복원합니다."""

        if not self.applied or self.completed:
            return
        _write_values(self.scene, self.previous_values)
        self.applied = False

    def complete(self) -> DatasetSnapshot:
        """Resource commit 성공을 확정하고 표시 Adapter에 한 번 알립니다."""

        if not self.applied:
            raise RuntimeError("적용하지 않은 공용 Dataset transaction은 완료할 수 없습니다.")
        self.completed = True
        result = snapshot(self.scene)
        if self.changed_display_fields:
            _notify_display_listeners(self.scene, result, self.changed_display_fields)
        return result


@dataclass
class DatasetRevisionTransaction:
    """Modifier 편집 Revision과 수량을 Scene 변경과 함께 rollback합니다."""

    scene: Any
    previous_values: dict[str, Any]
    next_values: dict[str, Any]
    applied: bool = False
    completed: bool = False

    def apply(self) -> None:
        """검증된 다음 Dataset Revision과 수량을 Scene에 기록합니다."""

        if self.applied:
            return
        _write_values(self.scene, self.next_values)
        self.applied = True

    def rollback(self) -> None:
        """완료 전 실패한 Dataset Revision과 수량을 이전 값으로 되돌립니다."""

        if not self.applied or self.completed:
            return
        _write_values(self.scene, self.previous_values)
        self.applied = False

    def complete(self) -> DatasetSnapshot:
        """Scene 편집 성공 뒤 공용 Dataset Revision 변경을 확정합니다."""

        if not self.applied:
            raise RuntimeError("적용하지 않은 공용 Dataset revision은 완료할 수 없습니다.")
        self.completed = True
        return snapshot(self.scene)


def _scene_pointer(scene: Any) -> int:
    """Blender Scene 또는 테스트 대역의 안정적인 Runtime 식별자를 반환합니다."""

    try:
        return int(scene.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return id(scene)


def _settings(scene: Any) -> Any:
    """등록된 Scene.colmap_dataset PropertyGroup을 검증해 반환합니다."""

    settings = getattr(scene, "colmap_dataset", None)
    if settings is None:
        raise RuntimeError("Scene.colmap_dataset 공용 Property가 등록되지 않았습니다.")
    return settings


def _snapshot_values(settings: Any) -> dict[str, Any]:
    """공용 Dataset Property 전체를 rollback 가능한 일반 값으로 복사합니다."""

    return {
        "dataset_uuid": str(settings.dataset_uuid),
        "dataset_revision": int(settings.dataset_revision),
        "display_revision": int(settings.display_revision),
        "source_fingerprint": str(settings.source_fingerprint),
        "dataset_root": str(settings.dataset_root),
        "sparse_root": str(settings.sparse_root),
        "model_directory": str(settings.model_directory),
        "images_directory": str(settings.images_directory),
        "input_format": str(settings.input_format),
        "layout": str(settings.layout),
        "selected_files_json": str(settings.selected_files_json),
        "image_path_manifest_json": str(settings.image_path_manifest_json),
        "camera_count": int(settings.camera_count),
        "image_count": int(settings.image_count),
        "point3d_count": int(settings.point3d_count),
        "schema_version": int(settings.schema_version),
        "source_schema_version": int(settings.source_schema_version),
        "axis_preset": str(settings.axis_preset),
        "axis_forward": str(settings.axis_forward),
        "axis_up": str(settings.axis_up),
        "point_size": float(settings.point_size),
        "camera_frustum_size": float(settings.camera_frustum_size),
    }


def _write_values(scene: Any, values: Mapping[str, Any]) -> None:
    """Property update 재진입을 막고 공용 Dataset 값 묶음을 Scene에 기록합니다."""

    settings = _settings(scene)
    pointer = _scene_pointer(scene)
    _SUPPRESSED_SCENE_POINTERS.add(pointer)
    try:
        for field_name, value in values.items():
            setattr(settings, field_name, value)
    finally:
        _SUPPRESSED_SCENE_POINTERS.discard(pointer)


def snapshot(scene: Any) -> DatasetSnapshot:
    """현재 Scene Property에서 읽기 전용 공용 Dataset Snapshot을 만듭니다."""

    values = _snapshot_values(_settings(scene))
    return DatasetSnapshot(**values)


def register_display_listener(tool_id: str, listener: DisplayListener) -> None:
    """도구별 공용 표시 속성 소비 Callback을 중복 없이 등록합니다."""

    if not tool_id or not callable(listener):
        raise ValueError("공용 Dataset 표시 Listener 등록값이 올바르지 않습니다.")
    existing = _DISPLAY_LISTENERS.get(tool_id)
    if existing is not None and existing is not listener:
        raise RuntimeError(f"{tool_id} 공용 Dataset 표시 Listener가 이미 등록되었습니다.")
    _DISPLAY_LISTENERS[tool_id] = listener


def unregister_display_listener(tool_id: str) -> None:
    """지정 도구의 공용 표시 속성 소비 Callback만 제거합니다."""

    _DISPLAY_LISTENERS.pop(tool_id, None)


def clear_display_listeners() -> None:
    """Extension 해제와 등록 실패에서 모든 공용 표시 Listener를 비웁니다."""

    _DISPLAY_LISTENERS.clear()
    _SUPPRESSED_SCENE_POINTERS.clear()


def _notify_display_listeners(
    scene: Any,
    current: DatasetSnapshot,
    changed_fields: frozenset[str],
) -> None:
    """등록 순서가 아닌 도구 ID 순서로 공용 표시 변경을 전달합니다."""

    for tool_id in sorted(_DISPLAY_LISTENERS):
        _DISPLAY_LISTENERS[tool_id](scene, current, changed_fields)


def notify_display_property_changed(
    settings: Any,
    context: Any,
    field_name: str,
) -> None:
    """직접 변경된 공용 표시 Property의 Revision을 올리고 Adapter에 알립니다."""

    notify_display_properties_changed(settings, context, frozenset({field_name}))


def notify_display_properties_changed(
    settings: Any,
    context: Any,
    field_names: frozenset[str],
) -> None:
    """직접 변경된 공용 표시 Property 묶음을 하나의 Revision으로 알립니다."""

    changed_fields = frozenset(str(field_name) for field_name in field_names)
    unknown = changed_fields - DISPLAY_FIELDS
    if unknown or not changed_fields:
        raise ValueError(f"지원하지 않는 공용 표시 속성입니다: {sorted(unknown)}")
    scene = getattr(settings, "id_data", None) or getattr(context, "scene", None)
    if scene is None or _scene_pointer(scene) in _SUPPRESSED_SCENE_POINTERS:
        return
    if not str(getattr(settings, "dataset_uuid", "")):
        return
    settings.display_revision = max(0, int(settings.display_revision)) + 1
    _notify_display_listeners(scene, snapshot(scene), changed_fields)


def update_display_settings(scene: Any, **changes: Any) -> DatasetSnapshot:
    """공용 표시값 묶음을 한 Revision으로 갱신하고 소비 Adapter에 알립니다."""

    unknown = frozenset(changes) - DISPLAY_FIELDS
    if unknown:
        raise ValueError(f"지원하지 않는 공용 표시 속성입니다: {sorted(unknown)}")
    current = snapshot(scene)
    if not current.available:
        return current
    changed = frozenset(
        field_name
        for field_name, value in changes.items()
        if getattr(current, field_name) != value
    )
    if not changed:
        return current
    values = _snapshot_values(_settings(scene))
    for field_name in changed:
        values[field_name] = changes[field_name]
    values["display_revision"] = current.display_revision + 1
    _write_values(scene, values)
    result = snapshot(scene)
    _notify_display_listeners(scene, result, changed)
    return result


def _same_model_directory(current: DatasetSnapshot, metadata: DatasetImportMetadata) -> bool:
    """경로 표현 차이를 제거해 같은 Source Model Directory인지 확인합니다."""

    if not current.model_directory:
        return False
    try:
        return Path(current.model_directory).resolve() == Path(metadata.model_directory).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def begin_import_commit(scene: Any, metadata: DatasetImportMetadata) -> DatasetMetadataTransaction:
    """Resource commit 전 적용·rollback할 공용 Dataset 메타데이터를 준비합니다."""

    current = snapshot(scene)
    previous_values = _snapshot_values(_settings(scene))
    same_dataset = bool(
        current.available
        and current.dataset_uuid == metadata.dataset_uuid
        and _same_model_directory(current, metadata)
    )
    display_values = {
        "axis_preset": metadata.axis_preset,
        "axis_forward": metadata.axis_forward,
        "axis_up": metadata.axis_up,
        "point_size": metadata.point_size,
        "camera_frustum_size": metadata.camera_frustum_size,
    }
    changed_display_fields = frozenset(
        field_name
        for field_name, value in display_values.items()
        if getattr(current, field_name) != value
    )
    next_values = {
        "dataset_uuid": metadata.dataset_uuid,
        "dataset_revision": current.dataset_revision + 1 if same_dataset else 1,
        "display_revision": (
            max(1, current.display_revision + (1 if changed_display_fields else 0))
            if same_dataset
            else 1
        ),
        "source_fingerprint": metadata.source_fingerprint,
        "dataset_root": metadata.dataset_root,
        "sparse_root": metadata.sparse_root,
        "model_directory": metadata.model_directory,
        "images_directory": metadata.images_directory,
        "input_format": metadata.input_format,
        "layout": metadata.layout,
        "selected_files_json": metadata.selected_files_json,
        "image_path_manifest_json": metadata.image_path_manifest_json,
        "camera_count": metadata.camera_count,
        "image_count": metadata.image_count,
        "point3d_count": metadata.point3d_count,
        "schema_version": DATASET_SCHEMA_VERSION,
        "source_schema_version": metadata.source_schema_version,
        **display_values,
    }
    return DatasetMetadataTransaction(
        scene=scene,
        metadata=metadata,
        previous_values=previous_values,
        next_values=next_values,
        changed_display_fields=changed_display_fields or DISPLAY_FIELDS,
    )


def begin_edit_commit(
    scene: Any,
    *,
    dataset_uuid: str,
    camera_count: int,
    image_count: int,
    point3d_count: int,
) -> DatasetRevisionTransaction:
    """Modifier 확정 편집의 공용 Dataset 수량과 새 Revision을 준비합니다."""

    current = snapshot(scene)
    if not current.available or current.dataset_uuid != dataset_uuid:
        raise RuntimeError("Modifier 편집 대상과 공용 Dataset UUID가 일치하지 않습니다.")
    previous_values = _snapshot_values(_settings(scene))
    next_values = dict(previous_values)
    next_values.update(
        dataset_revision=current.dataset_revision + 1,
        camera_count=int(camera_count),
        image_count=int(image_count),
        point3d_count=int(point3d_count),
    )
    return DatasetRevisionTransaction(
        scene=scene,
        previous_values=previous_values,
        next_values=next_values,
    )


def refresh_source_metadata(scene: Any, metadata: DatasetImportMetadata) -> DatasetSnapshot:
    """Geometry Revision을 바꾸지 않고 Export가 교체한 Source 기준값을 갱신합니다."""

    current = snapshot(scene)
    if not current.available or current.dataset_uuid != metadata.dataset_uuid:
        raise RuntimeError("Export 결과와 공용 Dataset UUID가 일치하지 않습니다.")
    values = _snapshot_values(_settings(scene))
    values.update(
        source_fingerprint=metadata.source_fingerprint,
        dataset_root=metadata.dataset_root,
        sparse_root=metadata.sparse_root,
        model_directory=metadata.model_directory,
        images_directory=metadata.images_directory,
        input_format=metadata.input_format,
        layout=metadata.layout,
        selected_files_json=metadata.selected_files_json,
        image_path_manifest_json=metadata.image_path_manifest_json,
        camera_count=metadata.camera_count,
        image_count=metadata.image_count,
        point3d_count=metadata.point3d_count,
        source_schema_version=metadata.source_schema_version,
    )
    _write_values(scene, values)
    refresh_resource_fingerprint(scene, metadata.source_fingerprint)
    return snapshot(scene)


def refresh_resource_fingerprint(scene: Any, source_fingerprint: str) -> int:
    """활성 공용 Dataset Blender ID의 Source 지문을 현재 Export 기준으로 갱신합니다."""

    current = snapshot(scene)
    if not current.available or not str(source_fingerprint).strip():
        raise RuntimeError("공용 Dataset Resource 지문 갱신 입력이 올바르지 않습니다.")
    roots = tuple(
        collection
        for collection in getattr(getattr(scene, "collection", None), "children", ())
        if is_dataset_owned(
            collection,
            dataset_uuid=current.dataset_uuid,
            role="ROOT",
        )
    )
    if len(roots) != 1:
        raise RuntimeError("활성 공용 Dataset Root Collection이 정확히 하나가 아닙니다.")
    root = roots[0]
    candidates: list[Any] = []

    def collect_collection(collection: Any) -> None:
        """공용 Root 아래 Collection과 연결 ID를 중복 허용 목록에 수집합니다."""

        candidates.append(collection)
        for child in tuple(getattr(collection, "children", ())):
            collect_collection(child)
        for obj in tuple(getattr(collection, "objects", ())):
            candidates.append(obj)
            data = getattr(obj, "data", None)
            if data is not None:
                candidates.append(data)
            for slot in tuple(getattr(obj, "material_slots", ())):
                material = getattr(slot, "material", None)
                if material is not None:
                    candidates.append(material)
            for modifier in tuple(getattr(obj, "modifiers", ())):
                node_group = getattr(modifier, "node_group", None)
                if node_group is not None:
                    candidates.append(node_group)

    collect_collection(root)
    updated = 0
    seen: set[int] = set()
    for data_block in candidates:
        try:
            pointer = int(data_block.as_pointer())
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pointer = id(data_block)
        if pointer in seen or not is_dataset_owned(
            data_block,
            dataset_uuid=current.dataset_uuid,
        ):
            continue
        seen.add(pointer)
        if str(data_block.get(SOURCE_FINGERPRINT_KEY, "")) == source_fingerprint:
            continue
        data_block[SOURCE_FINGERPRINT_KEY] = str(source_fingerprint)
        updated += 1
    return updated


def resources(scene: Any) -> DatasetResources | None:
    """현재 Scene의 중립 소유 태그에서 공용 Point·Camera·Axis ID를 검증해 찾습니다."""

    current = snapshot(scene)
    if not current.available:
        return None
    roots = tuple(
        collection
        for collection in getattr(getattr(scene, "collection", None), "children", ())
        if is_dataset_owned(collection, dataset_uuid=current.dataset_uuid, role="ROOT")
    )
    if len(roots) != 1:
        raise RuntimeError("활성 공용 Dataset Root Collection이 정확히 하나가 아닙니다.")
    root = roots[0]
    objects = tuple(getattr(root, "all_objects", ()))
    points = tuple(
        obj
        for obj in objects
        if is_dataset_owned(obj, dataset_uuid=current.dataset_uuid, role="POINT_MESH")
    )
    axes = tuple(
        obj
        for obj in objects
        if is_dataset_owned(obj, dataset_uuid=current.dataset_uuid, role="AXIS_ROOT")
    )
    if len(points) != 1 or len(axes) != 1:
        raise RuntimeError("공용 Dataset Point 또는 Axis Root가 정확히 하나가 아닙니다.")
    cameras: dict[int, Any] = {}
    camera_data_pointers: set[int] = set()
    for obj in objects:
        if not is_dataset_owned(obj, dataset_uuid=current.dataset_uuid, role="CAMERA"):
            continue
        try:
            image_id = int(obj.get(RECORD_ID_KEY, ""))
        except (TypeError, ValueError) as error:
            raise RuntimeError("공용 Dataset Camera의 IMAGE_ID가 올바르지 않습니다.") from error
        if image_id in cameras:
            raise RuntimeError(f"공용 Dataset IMAGE_ID {image_id} Camera가 중복되었습니다.")
        camera_data = getattr(obj, "data", None)
        if not is_dataset_owned(
            camera_data,
            dataset_uuid=current.dataset_uuid,
            role="CAMERA_DATA",
            record_id=image_id,
        ):
            raise RuntimeError(f"IMAGE_ID {image_id} Camera Data 소유권이 일치하지 않습니다.")
        try:
            pointer = int(camera_data.as_pointer())
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pointer = id(camera_data)
        if pointer in camera_data_pointers:
            raise RuntimeError("공용 Dataset Camera Data가 IMAGE_ID 사이에 공유되었습니다.")
        camera_data_pointers.add(pointer)
        cameras[image_id] = obj
    if len(cameras) != current.image_count:
        raise RuntimeError("공용 Dataset Camera Object 수가 Scene 메타데이터와 다릅니다.")
    return DatasetResources(
        root_collection=root,
        point_object=points[0],
        axis_root=axes[0],
        camera_objects=MappingProxyType(dict(sorted(cameras.items()))),
    )


__all__ = [
    "DISPLAY_FIELDS",
    "DatasetMetadataTransaction",
    "DatasetRevisionTransaction",
    "DatasetResources",
    "begin_edit_commit",
    "begin_import_commit",
    "clear_display_listeners",
    "notify_display_property_changed",
    "notify_display_properties_changed",
    "refresh_source_metadata",
    "refresh_resource_fingerprint",
    "register_display_listener",
    "resources",
    "snapshot",
    "unregister_display_listener",
    "update_display_settings",
]
