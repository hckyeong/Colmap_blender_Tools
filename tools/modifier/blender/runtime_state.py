"""원본 Reconstruction과 편집 이력을 연결하는 재생성 가능한 Runtime Cache입니다."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..core.edit_replay import history_hash, replay_history
from ..core.edits import AnyEditOperation, deserialize_history, serialize_history
from ..core.fingerprint import source_fingerprint
from ..core.indexes import RelationshipIndexes
from ..core.layout import resolve_layout
from ..core.loader import load_reconstruction
from ..core.model import (
    ImagePathManifest,
    LoadedReconstruction,
    Reconstruction,
    SourceDescriptor,
)
from ..core.export_types import ValidatedRuntimeToken
from ..core.export_validation import reconstruction_counts


@dataclass
class DatasetRuntimeState:
    dataset_uuid: str
    source: SourceDescriptor
    baseline_reconstruction: Reconstruction
    current_reconstruction: Reconstruction
    indexes: RelationshipIndexes
    operations: tuple[AnyEditOperation, ...]
    edit_revision: int
    index_revision: int
    history_hash: str
    point_id_order: tuple[int, ...]
    image_path_manifest: ImagePathManifest | None


_STATES: dict[str, DatasetRuntimeState] = {}


def _make_state(
    loaded: LoadedReconstruction,
    operations: tuple[AnyEditOperation, ...],
) -> DatasetRuntimeState:
    current, indexes = replay_history(
        loaded.reconstruction,
        operations,
        loaded.source.dataset_uuid,
        loaded.source.source_fingerprint,
        validation_token=loaded.validation_token,
    )
    return DatasetRuntimeState(
        dataset_uuid=loaded.source.dataset_uuid,
        source=loaded.source,
        baseline_reconstruction=loaded.reconstruction,
        current_reconstruction=current,
        indexes=indexes,
        operations=operations,
        edit_revision=len(operations),
        index_revision=len(operations),
        history_hash=history_hash(operations),
        point_id_order=tuple(sorted(current.points3d)),
        image_path_manifest=loaded.image_path_manifest,
    )


def prepare_import_state(settings, loaded: LoadedReconstruction) -> DatasetRuntimeState:
    """같은 원본 Reload에는 검증된 기존 이력을 재생하고 다른 원본은 초기화합니다."""
    operations: tuple[AnyEditOperation, ...] = ()
    same_dataset = (
        bool(settings.dataset_uuid)
        and settings.dataset_uuid == loaded.source.dataset_uuid
        and settings.active_model_directory
        and settings.active_model_directory == str(loaded.source.model_directory)
    )
    if same_dataset:
        operations = deserialize_history(settings.edit_history_json)
        if settings.edit_revision != len(operations):
            raise ValueError("저장된 편집 revision과 history 길이가 일치하지 않습니다.")
        expected_history_hash = history_hash(operations)
        legacy_empty_history = (
            not operations
            and settings.edit_revision == 0
            and not settings.history_hash
        )
        if not legacy_empty_history and settings.history_hash != expected_history_hash:
            raise ValueError("저장된 history hash가 편집 이력과 일치하지 않습니다.")
        if operations and settings.source_fingerprint != loaded.source.source_fingerprint:
            raise ValueError(
                "원본 source가 변경되어 기존 편집 이력을 안전하게 적용할 수 없습니다. "
                "Reset Edits 후 다시 Import하십시오."
            )
        if settings.source_fingerprint != loaded.source.source_fingerprint:
            operations = ()
    return _make_state(loaded, operations)


def state_from_export(loaded: LoadedReconstruction) -> DatasetRuntimeState:
    """디스크에 반영된 최종 모델을 빈 편집 이력의 새 기준선으로 전환합니다."""
    return _make_state(loaded, ())


def state_from_successful_export(
    state: DatasetRuntimeState,
    loaded: LoadedReconstruction,
) -> DatasetRuntimeState:
    """검증된 현재 모델과 관계 인덱스를 재순회하지 않고 새 기준선으로 전환합니다."""

    if loaded.reconstruction is not state.current_reconstruction:
        raise ValueError("Export 결과가 현재 Runtime Reconstruction 객체와 다릅니다.")
    if loaded.source.dataset_uuid != state.dataset_uuid:
        raise ValueError("Export 결과의 Dataset UUID가 현재 Runtime과 다릅니다.")
    if state.edit_revision != state.index_revision:
        raise ValueError("Runtime 모델과 관계 인덱스 revision이 일치하지 않습니다.")
    if len(state.point_id_order) != len(state.current_reconstruction.points3d):
        raise ValueError("Runtime Point3D 순서표와 현재 모델의 레코드 수가 다릅니다.")
    empty_operations: tuple[AnyEditOperation, ...] = ()
    return DatasetRuntimeState(
        dataset_uuid=state.dataset_uuid,
        source=loaded.source,
        baseline_reconstruction=state.current_reconstruction,
        current_reconstruction=state.current_reconstruction,
        indexes=state.indexes,
        operations=empty_operations,
        edit_revision=0,
        index_revision=0,
        history_hash=history_hash(empty_operations),
        point_id_order=state.point_id_order,
        image_path_manifest=loaded.image_path_manifest,
    )


def install_state(state: DatasetRuntimeState) -> None:
    if state.edit_revision != state.index_revision:
        raise ValueError(
            "Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다."
        )
    _STATES[state.dataset_uuid] = state


def clear_runtime() -> None:
    _STATES.clear()


def discard_runtime_for_global_undo() -> None:
    """Global Undo가 교체할 ID와 연결된 모든 재생성 가능 Runtime을 폐기합니다."""

    _STATES.clear()


def cached_runtime(dataset_uuid: str) -> DatasetRuntimeState | None:
    """파일 재검사 없이 현재 세션의 캐시된 Runtime만 반환합니다."""
    return _STATES.get(dataset_uuid)


def cached_runtime_for_pending(scene) -> DatasetRuntimeState:
    """Pending 등록에서 source 재해시 없이 현재 Runtime token만 검증합니다."""
    settings = scene.colmap_import
    if not settings.dataset_uuid:
        raise ValueError("먼저 COLMAP 데이터셋을 Import하십시오.")
    cached = _STATES.get(settings.dataset_uuid)
    if cached is None:
        raise ValueError(
            "현재 Runtime Cache가 없습니다. Import 또는 파일 재로드 복원을 먼저 완료하십시오."
        )
    if (
        cached.dataset_uuid != settings.dataset_uuid
        or cached.source.source_fingerprint != settings.source_fingerprint
        or cached.edit_revision != settings.edit_revision
        or cached.index_revision != cached.edit_revision
        or cached.history_hash != settings.history_hash
    ):
        raise ValueError(
            "현재 Scene과 Runtime Cache token이 일치하지 않습니다. 다시 Import하십시오."
        )
    return cached


def validated_runtime_token(state: DatasetRuntimeState) -> ValidatedRuntimeToken:
    """편집 경계를 통과한 Runtime을 Export용 불변 token으로 고정합니다."""

    if not state.operations:
        raise ValueError("Export할 편집 내역이 없습니다.")
    if state.edit_revision != len(state.operations):
        raise ValueError("Runtime 편집 revision과 history 길이가 일치하지 않습니다.")
    if state.index_revision != state.edit_revision:
        raise ValueError("Runtime 모델과 관계 인덱스 revision이 일치하지 않습니다.")
    if state.history_hash != history_hash(state.operations):
        raise ValueError("Runtime history hash가 편집 이력과 일치하지 않습니다.")
    return ValidatedRuntimeToken(
        dataset_uuid=state.dataset_uuid,
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
        reconstruction_identity=id(state.current_reconstruction),
        model_counts=reconstruction_counts(state.current_reconstruction),
    )


def cached_runtime_for_export(scene) -> DatasetRuntimeState:
    """Source 재해시 없이 Scene·Runtime history token을 Export 전에 검증합니다."""

    settings = scene.colmap_import
    state = cached_runtime_for_pending(scene)
    operations = deserialize_history(settings.edit_history_json)
    if (
        len(operations) != settings.edit_revision
        or history_hash(operations) != settings.history_hash
        or operations != state.operations
    ):
        raise ValueError(
            "Scene의 편집 이력과 Runtime Cache가 일치하지 않습니다. 다시 Import하십시오."
        )
    if (
        settings.active_model_directory != str(state.source.model_directory)
        or settings.active_input_format != state.source.input_format
        or settings.active_layout != state.source.layout
    ):
        raise ValueError(
            "Scene의 source 경로 또는 형식이 Runtime Cache와 일치하지 않습니다. "
            "다시 Import하십시오."
        )
    validated_runtime_token(state)
    return state


def assert_export_runtime_token(
    scene,
    expected: ValidatedRuntimeToken,
) -> DatasetRuntimeState:
    """확인창 이후 Runtime이 바뀌지 않았는지 source 재해시 없이 검사합니다."""

    state = cached_runtime_for_export(scene)
    if validated_runtime_token(state) != expected:
        raise ValueError(
            "Export 확인 이후 편집 상태가 변경되었습니다. Export를 다시 실행하십시오."
        )
    return state


def loaded_for_current(state: DatasetRuntimeState) -> LoadedReconstruction:
    return LoadedReconstruction(
        state.current_reconstruction,
        state.source,
        (),
        image_path_manifest=state.image_path_manifest,
    )


def _verify_source(settings) -> None:
    layout = resolve_layout(settings.active_model_directory, settings.active_input_format.upper())
    current = source_fingerprint(layout)
    if current != settings.source_fingerprint:
        raise ValueError(
            "Import 이후 COLMAP source 파일이 변경되었습니다. 기존 편집을 중단하고 "
            "Reset Edits 후 다시 Import하십시오."
        )


def ensure_runtime(scene) -> DatasetRuntimeState:
    settings = scene.colmap_import
    if not settings.dataset_uuid or not settings.active_model_directory:
        raise ValueError("먼저 COLMAP 데이터셋을 Import하십시오.")
    _verify_source(settings)
    operations = deserialize_history(settings.edit_history_json)
    expected_hash = history_hash(operations)
    if settings.history_hash != expected_hash:
        raise ValueError("Scene의 편집 이력 hash가 일치하지 않습니다.")
    cached = _STATES.get(settings.dataset_uuid)
    if (
        cached is not None
        and cached.source.source_fingerprint == settings.source_fingerprint
        and cached.edit_revision == settings.edit_revision
        and cached.history_hash == expected_hash
    ):
        if cached.index_revision != cached.edit_revision:
            raise ValueError(
                "Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다."
            )
        return cached

    loaded = load_reconstruction(
        settings.active_dataset_root,
        settings.active_model_directory,
        settings.active_images_directory,
        settings.active_input_format.upper(),
        dataset_uuid=settings.dataset_uuid,
    )
    if loaded.source.source_fingerprint != settings.source_fingerprint:
        raise ValueError("저장된 source fingerprint와 현재 원본이 일치하지 않습니다.")
    rebuilt = _make_state(loaded, operations)
    if rebuilt.edit_revision != settings.edit_revision:
        raise ValueError("Scene의 편집 revision과 이력 길이가 일치하지 않습니다.")
    install_state(rebuilt)
    return rebuilt


def state_with_current(
    state: DatasetRuntimeState,
    current: Reconstruction,
    indexes: RelationshipIndexes,
    operations: tuple[AnyEditOperation, ...],
) -> DatasetRuntimeState:
    revision = len(operations)
    return DatasetRuntimeState(
        dataset_uuid=state.dataset_uuid,
        source=state.source,
        baseline_reconstruction=state.baseline_reconstruction,
        current_reconstruction=current,
        indexes=indexes,
        operations=operations,
        edit_revision=revision,
        index_revision=revision,
        history_hash=history_hash(operations),
        point_id_order=tuple(sorted(current.points3d)),
        image_path_manifest=state.image_path_manifest,
    )


def write_history_settings(settings, state: DatasetRuntimeState) -> None:
    settings.edit_history_json = serialize_history(state.operations)
    settings.edit_revision = state.edit_revision
    settings.history_hash = state.history_hash
    settings.last_operation_id = (
        state.operations[-1].operation_id if state.operations else ""
    )


def write_source_settings(settings, state: DatasetRuntimeState) -> None:
    source = state.source
    settings.active_dataset_root = str(source.dataset_root)
    settings.active_sparse_root = str(source.sparse_root)
    settings.active_model_directory = str(source.model_directory)
    settings.active_images_directory = str(source.images_directory)
    settings.active_input_format = source.input_format
    settings.active_layout = source.layout
    settings.source_fingerprint = source.source_fingerprint
    settings.dataset_uuid = source.dataset_uuid
    settings.schema_version = str(source.schema_version)
    settings.selected_files_json = json.dumps(
        [str(path) for path in source.selected_files],
        ensure_ascii=False,
    )
