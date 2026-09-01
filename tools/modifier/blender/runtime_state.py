"""원본 Reconstruction과 편집 이력을 연결하는 재생성 가능한 Runtime Cache입니다."""

from __future__ import annotations

import json
import heapq
import time
from dataclasses import dataclass
from typing import Any

from ..core.edit_replay import history_hash, replay_history, replay_operation_indexed
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
from ..core.runtime_restore_job import (
    RuntimeSourceRestoreJob,
    RuntimeSourceRestorePlan,
)


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
_RESTORE_JOBS: dict[int, "RuntimeRestoreJob"] = {}


@dataclass(frozen=True, slots=True)
class RuntimeRestoreToken:
    """Runtime 복원 중 변경되면 작업을 폐기할 Scene 권위값을 보관합니다."""

    scene_pointer: int
    dataset_uuid: str
    edit_revision: int
    history_hash: str
    source_fingerprint: str
    dataset_root: str
    model_directory: str
    images_directory: str
    input_format: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class RuntimeRestoreProgress:
    """Modifier와 공용 Provider가 표시할 단조 증가 복원 진행값입니다."""

    phase: str
    fraction: float
    completed_units: int
    total_units: int
    message: str


def _pointer(value: Any) -> int:
    """Blender RNA 객체의 현재 포인터를 예외 없이 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def runtime_restore_token(scene: Any) -> RuntimeRestoreToken:
    """현재 Scene의 Runtime 복원 권위 token을 불변 값으로 캡처합니다."""

    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        raise ValueError("COLMAP Modifier Scene 설정이 없습니다.")
    dataset_uuid = str(settings.dataset_uuid or "")
    model_directory = str(settings.active_model_directory or "")
    if not dataset_uuid or not model_directory:
        raise ValueError("먼저 COLMAP 데이터셋을 Import하십시오.")
    try:
        schema_version = int(str(settings.schema_version or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "저장된 COLMAP Source Schema가 없습니다. "
            "COLMAP 데이터를 다시 Import하십시오."
        ) from exc
    return RuntimeRestoreToken(
        scene_pointer=_pointer(scene),
        dataset_uuid=dataset_uuid,
        edit_revision=int(settings.edit_revision),
        history_hash=str(settings.history_hash or ""),
        source_fingerprint=str(settings.source_fingerprint or ""),
        dataset_root=str(settings.active_dataset_root or ""),
        model_directory=model_directory,
        images_directory=str(settings.active_images_directory or ""),
        input_format=str(settings.active_input_format or "AUTO").upper(),
        schema_version=schema_version,
    )


def _restore_token_is_current(scene: Any, token: RuntimeRestoreToken) -> bool:
    """복원 시작 시 캡처한 Scene 경로·Revision token이 아직 같은지 확인합니다."""

    try:
        return runtime_restore_token(scene) == token
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def cached_runtime_matches_scene(scene: Any) -> bool:
    """파일 재해시 없이 전역 Runtime과 Scene 편집 token의 일치 여부를 반환합니다."""

    try:
        token = runtime_restore_token(scene)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    cached = _STATES.get(token.dataset_uuid)
    return bool(
        cached is not None
        and cached.dataset_uuid == token.dataset_uuid
        and cached.source.source_fingerprint == token.source_fingerprint
        and cached.edit_revision == token.edit_revision
        and cached.index_revision == token.edit_revision
        and cached.history_hash == token.history_hash
    )


class _RelationshipIndexBuildJob:
    """대용량 관계 인덱스를 Point·Image·Frame 레코드 배치로 생성합니다."""

    def __init__(self, model: Reconstruction) -> None:
        """검증된 Reconstruction의 반복자와 빈 인덱스 버퍼를 준비합니다."""

        self._model = model
        self._point_items = iter(model.points3d.items())
        self._image_items = iter(model.images.items())
        self._frame_items = iter(model.frames.values())
        self._current_point_id: int | None = None
        self._current_point_tracks: Any = None
        self._current_point_images: set[int] = set()
        self._current_image_id: int | None = None
        self._current_image_camera_id: int | None = None
        self._current_image_points: Any = None
        self._current_image_point_ids: set[int] = set()
        self._current_frame_id: int | None = None
        self._current_frame_data_ids: Any = None
        self._phase = "POINTS"
        self._point_to_images: dict[int, frozenset[int]] = {}
        self._image_to_points: dict[int, frozenset[int]] = {}
        self._camera_sets: dict[int, set[int]] = {
            int(camera_id): set() for camera_id in model.cameras
        }
        self._image_to_frame: dict[int, int] = {}
        self._total_units = (
            len(model.points3d) + len(model.images) + len(model.frames)
        )
        self._completed_units = 0
        self.done = self._total_units == 0
        self.result: RelationshipIndexes | None = None
        if self.done:
            self._finish()

    @property
    def fraction(self) -> float:
        """현재까지 처리한 인덱스 레코드 비율을 반환합니다."""

        if self._total_units <= 0:
            return 1.0
        return min(1.0, self._completed_units / self._total_units)

    @property
    def completed_units(self) -> int:
        """완료한 Point·Image·Frame 레코드 수를 반환합니다."""

        return self._completed_units

    @property
    def total_units(self) -> int:
        """인덱스 구축 대상 전체 레코드 수를 반환합니다."""

        return self._total_units

    def advance(self, work_budget_ms: float = 20.0, hard_cap: int = 4096) -> None:
        """시간 예산과 최대 레코드 수 안에서 인덱스 구축을 전진시킵니다."""

        if self.done:
            return
        deadline = time.monotonic() + max(0.001, float(work_budget_ms) / 1000.0)
        processed = 0
        while processed < int(hard_cap) and time.monotonic() < deadline:
            if self._phase == "POINTS":
                if self._current_point_id is None:
                    try:
                        point_id, point = next(self._point_items)
                    except StopIteration:
                        self._phase = "IMAGES"
                        continue
                    self._current_point_id = int(point_id)
                    self._current_point_tracks = iter(point.track)
                    self._current_point_images = set()
                try:
                    track = next(self._current_point_tracks)
                except StopIteration:
                    self._point_to_images[self._current_point_id] = frozenset(
                        self._current_point_images
                    )
                    self._current_point_id = None
                    self._current_point_tracks = None
                    self._current_point_images = set()
                    self._completed_units += 1
                    continue
                self._current_point_images.add(int(track.image_id))
            elif self._phase == "IMAGES":
                if self._current_image_id is None:
                    try:
                        image_id, image = next(self._image_items)
                    except StopIteration:
                        self._phase = "FRAMES"
                        continue
                    self._current_image_id = int(image_id)
                    self._current_image_camera_id = int(image.camera_id)
                    self._current_image_points = iter(image.points2d)
                    self._current_image_point_ids = set()
                try:
                    point = next(self._current_image_points)
                except StopIteration:
                    self._image_to_points[self._current_image_id] = frozenset(
                        self._current_image_point_ids
                    )
                    self._camera_sets[self._current_image_camera_id].add(
                        self._current_image_id
                    )
                    self._current_image_id = None
                    self._current_image_camera_id = None
                    self._current_image_points = None
                    self._current_image_point_ids = set()
                    self._completed_units += 1
                    continue
                if point.point3d_id is not None:
                    self._current_image_point_ids.add(int(point.point3d_id))
            elif self._phase == "FRAMES":
                if self._current_frame_id is None:
                    try:
                        frame = next(self._frame_items)
                    except StopIteration:
                        self._finish()
                        return
                    self._current_frame_id = int(frame.frame_id)
                    self._current_frame_data_ids = iter(frame.data_ids)
                try:
                    data_id = next(self._current_frame_data_ids)
                except StopIteration:
                    self._current_frame_id = None
                    self._current_frame_data_ids = None
                    self._completed_units += 1
                    continue
                if int(data_id.sensor_type) == 0:
                    image_id = int(data_id.data_id)
                    if image_id in self._image_to_frame:
                        raise ValueError(
                            f"IMAGE_ID {image_id}가 여러 Frame에 연결되어 있습니다."
                        )
                    self._image_to_frame[image_id] = self._current_frame_id
            else:
                raise RuntimeError("알 수 없는 관계 인덱스 구축 단계입니다.")
            processed += 1

    def _finish(self) -> None:
        """완료된 가변 버퍼를 Runtime용 관계 인덱스로 고정합니다."""

        self.result = RelationshipIndexes(
            point_to_images=self._point_to_images,
            image_to_points=self._image_to_points,
            camera_to_images={
                camera_id: frozenset(image_ids)
                for camera_id, image_ids in self._camera_sets.items()
            },
            image_to_frame=self._image_to_frame,
        )
        self._completed_units = self._total_units
        self.done = True


class RuntimeRestoreJob:
    """저장된 Modifier Scene에서 Runtime을 증분 복원하고 마지막에만 설치합니다."""

    def __init__(self, scene: Any, token: RuntimeRestoreToken) -> None:
        """Scene token과 전용 빠른 source Job으로 독립 복원 작업을 준비합니다."""

        if not _restore_token_is_current(scene, token):
            raise ValueError("Runtime 복원 시작 token이 현재 Scene과 일치하지 않습니다.")
        self.scene = scene
        self.token = token
        settings = scene.colmap_import
        self._operations = deserialize_history(settings.edit_history_json)
        if len(self._operations) != token.edit_revision:
            raise ValueError("Scene의 편집 revision과 이력 길이가 일치하지 않습니다.")
        if history_hash(self._operations) != token.history_hash:
            raise ValueError("Scene의 편집 이력 hash가 일치하지 않습니다.")
        self._load_job = RuntimeSourceRestoreJob(
            RuntimeSourceRestorePlan(
                dataset_root=token.dataset_root,
                model_directory=token.model_directory,
                images_directory=token.images_directory,
                input_format=token.input_format,
                expected_dataset_uuid=token.dataset_uuid,
                expected_source_fingerprint=token.source_fingerprint,
                expected_schema_version=token.schema_version,
            )
        )
        self._index_job: _RelationshipIndexBuildJob | None = None
        self._loaded: LoadedReconstruction | None = None
        self._current: Reconstruction | None = None
        self._indexes: RelationshipIndexes | None = None
        self._operation_index = 0
        self._expected_revision = 0
        self._point_iterator: Any = None
        self._point_chunks: list[list[int]] = []
        self._point_merge: Any = None
        self._point_id_order: list[int] = []
        self._phase = "LOAD"
        self.done = False
        self.failed = False
        self.error: Exception | None = None
        self.result: DatasetRuntimeState | None = None
        self.progress = RuntimeRestoreProgress(
            "LOAD",
            0.0,
            0,
            0,
            "저장된 원본 확인 중",
        )

    def advance(self, work_budget_ms: float = 20.0) -> RuntimeRestoreProgress:
        """한 UI Tick의 예산 안에서 복원 단계를 전진시킵니다."""

        if self.done or self.failed:
            return self.progress
        if not _restore_token_is_current(self.scene, self.token):
            error = ValueError("Runtime 복원 중 Scene의 source 또는 편집 token이 변경되었습니다.")
            self._fail(error)
            raise error
        try:
            if self._phase == "LOAD":
                self._advance_load(work_budget_ms)
            elif self._phase == "INDEX":
                self._advance_indexes(work_budget_ms)
            elif self._phase == "HISTORY":
                self._advance_history()
            elif self._phase == "POINT_ORDER":
                self._advance_point_order(work_budget_ms)
            elif self._phase == "COMMIT":
                self._commit()
            else:
                raise RuntimeError("알 수 없는 Modifier Runtime 복원 단계입니다.")
        except Exception as exc:
            self._fail(exc)
            raise
        return self.progress

    def abort(self) -> None:
        """미완료 Loader와 Staging 버퍼를 폐기하고 전역 Runtime은 유지합니다."""

        if self.done or self.failed:
            return
        self._load_job.abort()
        self.failed = True
        self.error = RuntimeError("Modifier Runtime 복원이 취소되었습니다.")
        self.progress = RuntimeRestoreProgress(
            self._phase,
            self.progress.fraction,
            self.progress.completed_units,
            self.progress.total_units,
            "Modifier Runtime 복원 취소",
        )

    def _advance_load(self, work_budget_ms: float) -> None:
        """전용 Core Job으로 단일 해시·파싱·이미지 경로 확인을 실행합니다."""

        if not self._load_job.done:
            core = self._load_job.advance(work_budget_ms=work_budget_ms)
            self.progress = RuntimeRestoreProgress(
                "LOAD",
                min(0.74, float(core.overall_fraction) * 0.74),
                int(core.completed_units),
                int(core.total_units),
                str(core.message),
            )
            return
        loaded = self._load_job.result
        if (
            loaded.source.dataset_uuid != self.token.dataset_uuid
            or loaded.source.source_fingerprint != self.token.source_fingerprint
            or str(loaded.source.model_directory) != self.token.model_directory
            or str(loaded.source.images_directory) != self.token.images_directory
        ):
            raise ValueError("저장된 Modifier source token과 현재 원본이 일치하지 않습니다.")
        validation_token = loaded.validation_token
        if validation_token is None or validation_token.reconstruction is not loaded.reconstruction:
            raise ValueError("검증된 Reconstruction token이 없습니다.")
        self._loaded = loaded
        self._current = loaded.reconstruction
        self._index_job = _RelationshipIndexBuildJob(loaded.reconstruction)
        self._phase = "INDEX"
        self.progress = RuntimeRestoreProgress(
            "INDEX",
            0.74,
            0,
            self._index_job.total_units,
            "편집 관계 준비 중",
        )

    def _advance_indexes(self, work_budget_ms: float) -> None:
        """Point·Image·Frame 관계 인덱스를 제한된 레코드 배치로 생성합니다."""

        if self._index_job is None:
            raise RuntimeError("Runtime 관계 인덱스 Job이 없습니다.")
        self._index_job.advance(work_budget_ms)
        self.progress = RuntimeRestoreProgress(
            "INDEX",
            0.74 + (self._index_job.fraction * 0.14),
            self._index_job.completed_units,
            self._index_job.total_units,
            "편집 관계 준비 중",
        )
        if not self._index_job.done:
            return
        if self._index_job.result is None:
            raise RuntimeError("Runtime 관계 인덱스 결과가 없습니다.")
        self._indexes = self._index_job.result
        self._phase = "HISTORY" if self._operations else "POINT_ORDER"

    def _advance_history(self) -> None:
        """저장된 편집 이력을 한 Tick에 한 operation씩 재생합니다."""

        if self._current is None or self._indexes is None:
            raise RuntimeError("편집 이력을 적용할 Runtime 기준선이 없습니다.")
        operation = self._operations[self._operation_index]
        self._current, self._indexes, self._expected_revision = replay_operation_indexed(
            self._current,
            self._indexes,
            operation,
            dataset_uuid=self.token.dataset_uuid,
            source_fingerprint=self.token.source_fingerprint,
            expected_revision=self._expected_revision,
        )
        self._operation_index += 1
        total = len(self._operations)
        self.progress = RuntimeRestoreProgress(
            "HISTORY",
            0.88 + (self._operation_index / total * 0.08),
            self._operation_index,
            total,
            "저장된 편집 내용 적용 중",
        )
        if self._operation_index >= total:
            self._phase = "POINT_ORDER"

    def _advance_point_order(self, work_budget_ms: float) -> None:
        """Point ID 정렬을 작은 정렬 chunk와 merge 소비 배치로 나눕니다."""

        if self._current is None:
            raise RuntimeError("Point ID 순서를 만들 Reconstruction이 없습니다.")
        deadline = time.monotonic() + max(0.001, float(work_budget_ms) / 1000.0)
        total = len(self._current.points3d)
        if self._point_iterator is None and self._point_merge is None:
            self._point_iterator = iter(self._current.points3d)
        while self._point_iterator is not None and time.monotonic() < deadline:
            chunk: list[int] = []
            try:
                for _index in range(4096):
                    chunk.append(int(next(self._point_iterator)))
            except StopIteration:
                self._point_iterator = None
            if chunk:
                chunk.sort()
                self._point_chunks.append(chunk)
            if self._point_iterator is None:
                self._point_merge = heapq.merge(*self._point_chunks)
                self._point_chunks.clear()
                break
        if self._point_merge is not None:
            for _index in range(4096):
                if time.monotonic() >= deadline:
                    break
                try:
                    self._point_id_order.append(int(next(self._point_merge)))
                except StopIteration:
                    self._point_merge = None
                    self._phase = "COMMIT"
                    break
        completed = len(self._point_id_order)
        self.progress = RuntimeRestoreProgress(
            "POINT_ORDER",
            0.96 + ((completed / total if total else 1.0) * 0.035),
            completed,
            total,
            "Point3D 순서 준비 중",
        )
        if total == 0:
            self._phase = "COMMIT"

    def _commit(self) -> None:
        """완성된 Runtime을 Scene token 재검증 뒤 전역 Registry에 한 번만 설치합니다."""

        if (
            self._loaded is None
            or self._current is None
            or self._indexes is None
            or not _restore_token_is_current(self.scene, self.token)
        ):
            raise ValueError("Modifier Runtime 완료 token 검증에 실패했습니다.")
        if self._expected_revision != self.token.edit_revision:
            raise ValueError("복원된 편집 revision이 Scene token과 일치하지 않습니다.")
        rebuilt = DatasetRuntimeState(
            dataset_uuid=self.token.dataset_uuid,
            source=self._loaded.source,
            baseline_reconstruction=self._loaded.reconstruction,
            current_reconstruction=self._current,
            indexes=self._indexes,
            operations=self._operations,
            edit_revision=self.token.edit_revision,
            index_revision=self.token.edit_revision,
            history_hash=self.token.history_hash,
            point_id_order=tuple(self._point_id_order),
            image_path_manifest=self._loaded.image_path_manifest,
        )
        install_state(rebuilt)
        self.result = rebuilt
        self.done = True
        self.progress = RuntimeRestoreProgress(
            "DONE",
            1.0,
            1,
            1,
            "Modifier Runtime 복원 완료",
        )

    def _fail(self, error: Exception) -> None:
        """실패 원인을 보존하고 열린 증분 Loader만 정리합니다."""

        if self.done or self.failed:
            return
        if not self._load_job.done and not self._load_job.failed:
            self._load_job.abort()
        self.failed = True
        self.error = error
        self.progress = RuntimeRestoreProgress(
            self._phase,
            self.progress.fraction,
            self.progress.completed_units,
            self.progress.total_units,
            f"Modifier Runtime 복원 실패: {error}",
        )


def request_runtime_restore(scene: Any) -> RuntimeRestoreJob:
    """같은 Scene token의 요청을 하나의 Runtime 복원 Job으로 조정합니다."""

    token = runtime_restore_token(scene)
    key = token.scene_pointer
    current = _RESTORE_JOBS.get(key)
    if current is not None and current.token == token and not current.failed:
        return current
    if current is not None and not current.done:
        current.abort()
    job = RuntimeRestoreJob(scene, token)
    _RESTORE_JOBS[key] = job
    return job


def runtime_restore_job(scene: Any) -> RuntimeRestoreJob | None:
    """현재 Scene에 연결된 증분 Runtime 복원 Job을 반환합니다."""

    return _RESTORE_JOBS.get(_pointer(scene))


def cancel_runtime_restore(scene: Any | None = None) -> None:
    """지정 Scene 또는 모든 미완료 Runtime 복원 Job을 안전하게 취소합니다."""

    if scene is None:
        jobs = tuple(_RESTORE_JOBS.values())
        _RESTORE_JOBS.clear()
    else:
        job = _RESTORE_JOBS.pop(_pointer(scene), None)
        jobs = (job,) if job is not None else ()
    for job in jobs:
        if not job.done and not job.failed:
            job.abort()


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
    """완전히 검증된 Runtime 상태만 Dataset UUID Registry에 설치합니다."""

    if state.edit_revision != state.index_revision:
        raise ValueError(
            "Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다."
        )
    _STATES[state.dataset_uuid] = state


def clear_runtime() -> None:
    """모든 증분 복원 작업을 취소하고 설치된 Modifier Runtime을 폐기합니다."""

    cancel_runtime_restore()
    _STATES.clear()


def discard_runtime_for_global_undo() -> None:
    """Global Undo가 교체할 ID와 연결된 모든 재생성 가능 Runtime을 폐기합니다."""

    cancel_runtime_restore()
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
