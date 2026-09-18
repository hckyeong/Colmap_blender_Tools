"""삼각형 OBJ를 검증·GPU 준비·Data Layer 등록까지 원자적으로 처리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from time import perf_counter
from typing import Any, BinaryIO, Callable

import numpy as np

from .constants import (
    GEOMETRY_KIND_TRIANGLE_MESH,
    OBJ_DISPLAY_VERTEX_LIMIT,
)
from .display_axes import (
    DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS,
    DEFAULT_UP_AXIS,
    ModelDisplayTransform,
    create_model_display_transform,
)
from .mesh_io import TriangleMeshModelData
from .mesh_io.embedded_obj_store import (
    commit_embedded_obj_mesh_and_build_proxy,
)
from .mesh_io.obj_native_import import NativeObjStaging, import_obj_to_staging
from .mesh_io.validation import validate_obj_limits
from .model_repository import (
    ImportedModelSourceMetadata,
    ModelRepositoryError,
    canonical_source_path,
    create_model_id,
)
from .viewport.display_sampling import (
    DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE,
    IncrementalDisplayIndexBuilder,
    normalize_display_point_limit,
)


MESH_IMPORT_STAGE_FILE_CHECK = "FILE_CHECK"
MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE = "HASH_SOURCE_BEFORE"
MESH_IMPORT_STAGE_READ_OBJ = "READ_OBJ"
MESH_IMPORT_STAGE_VALIDATE = "VALIDATE_MESH"
MESH_IMPORT_STAGE_HASH_SOURCE_AFTER = "HASH_SOURCE_AFTER"
MESH_IMPORT_STAGE_PREPARE_MODEL = "PREPARE_MODEL"
MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE = "PREPARE_DISPLAY_SAMPLE"
MESH_IMPORT_STAGE_PREPARE_GPU = "PREPARE_GPU"
MESH_IMPORT_STAGE_COMMIT = "COMMIT"
MESH_IMPORT_STAGE_COMPLETED = "COMPLETED"
MESH_IMPORT_STAGE_CANCELLED = "CANCELLED"
MESH_IMPORT_STAGE_FAILED = "FAILED"
_MESH_TERMINAL_STAGES = frozenset(
    {MESH_IMPORT_STAGE_COMPLETED, MESH_IMPORT_STAGE_CANCELLED, MESH_IMPORT_STAGE_FAILED}
)


class MeshImportJobError(RuntimeError):
    """OBJ 가져오기 단계와 원자 등록 경계의 오류를 나타냅니다."""


def mesh_import_stage_label(stage: str) -> str:
    """OBJ 가져오기 내부 단계를 사용자 표시용 한국어 문구로 변환합니다."""
    return {
        MESH_IMPORT_STAGE_FILE_CHECK: "파일 검사",
        MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE: "가져오기 전 원본 확인",
        MESH_IMPORT_STAGE_READ_OBJ: "OBJ 형상·UV 읽기",
        MESH_IMPORT_STAGE_VALIDATE: "정점·삼각면·UV 검증",
        MESH_IMPORT_STAGE_HASH_SOURCE_AFTER: "가져오기 후 원본 재확인",
        MESH_IMPORT_STAGE_PREPARE_MODEL: "메시 원본 배열 준비",
        MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE: "마커 표시 정점 선택",
        MESH_IMPORT_STAGE_PREPARE_GPU: "GPU 삼각면 배치 준비",
        MESH_IMPORT_STAGE_COMMIT: "Data Layers 등록",
        MESH_IMPORT_STAGE_COMPLETED: "가져오기 완료",
        MESH_IMPORT_STAGE_CANCELLED: "가져오기 취소",
        MESH_IMPORT_STAGE_FAILED: "가져오기 실패",
    }.get(str(stage), "알 수 없는 가져오기 단계")


class _FileFingerprintJob:
    """Blender TIMER 시간 예산 안에서 OBJ SHA-256을 증분 계산합니다."""

    def __init__(self, path: Path) -> None:
        """지문 대상 파일과 전체 바이트 수를 고정하고 스트림을 엽니다."""
        self.path = Path(path)
        try:
            self.total_bytes = int(self.path.stat().st_size)
            self._stream: BinaryIO | None = self.path.open("rb")
        except OSError as error:
            raise MeshImportJobError(f"OBJ source fingerprint를 계산하지 못했습니다: {path}") from error
        self.completed_bytes = 0
        self.done = False
        self.result = ""
        self._digest = hashlib.sha256()

    def close(self) -> None:
        """열린 OBJ 바이너리 스트림을 한 번만 닫습니다."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()

    def step(self, time_budget_seconds: float) -> None:
        """최소 한 청크를 읽고 지정 시간 예산에 도달하면 다음 TIMER로 넘깁니다."""
        if self.done:
            return
        budget = float(time_budget_seconds)
        if not np.isfinite(budget) or budget < 0.0:
            raise MeshImportJobError("OBJ fingerprint 시간 예산이 올바르지 않습니다.")
        stream = self._stream
        if stream is None:
            raise MeshImportJobError("OBJ fingerprint 스트림이 닫혀 있습니다.")
        started_at = perf_counter()
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                self.result = self._digest.hexdigest()
                self.done = True
                self.close()
                return
            self._digest.update(chunk)
            self.completed_bytes += len(chunk)
            if perf_counter() - started_at >= budget:
                return


def _prepare_gpu_batch_for_model(model_id: str, model_data: TriangleMeshModelData) -> None:
    """렌더러 공개 경계로 메시 삼각면·정점 overlay GPU cache를 준비합니다."""
    from .viewport import renderer

    prepare = getattr(renderer, "prepare_gpu_batch_for_model", None)
    if not callable(prepare):
        raise MeshImportJobError("메시 GPU 배치 준비 함수를 찾을 수 없습니다.")
    prepare(model_id, model_data)


def _discard_gpu_batch_for_model(model_id: str) -> None:
    """가져오기 실패·취소 때 해당 메시의 GPU cache만 제거합니다."""
    from .viewport import renderer

    discard = getattr(renderer, "discard_gpu_batch_for_model", None)
    if callable(discard):
        discard(model_id)


@dataclass
class MeshImportJob:
    """Blender TIMER가 삼각형 OBJ를 단계별로 확정하는 작업 상태입니다."""

    source_path: str
    model_id_factory: Callable[[], str] = create_model_id
    axis_preset: str = DEFAULT_AXIS_PRESET
    axis_forward: str = DEFAULT_FORWARD_AXIS
    axis_up: str = DEFAULT_UP_AXIS
    display_vertex_limit: int = OBJ_DISPLAY_VERTEX_LIMIT
    display_name_hint: str = ""
    expected_source_fingerprint: str = ""
    dataset_root: str = ""
    stage: str = MESH_IMPORT_STAGE_FILE_CHECK
    progress: float = 0.0
    current_count: int = 0
    total_count: int = 0
    cancel_requested: bool = False
    staged_model: TriangleMeshModelData | None = None
    error_message: str = ""
    result_message: str = ""
    source_fingerprint_before: str = field(default="", repr=False)
    parsed_result: Any = field(default=None, repr=False)
    display_indices: np.ndarray | None = field(default=None, repr=False)
    gpu_batch_prepared: bool = field(default=False, repr=False)
    started_at: float = field(default_factory=perf_counter, repr=False)
    display_transform: ModelDisplayTransform = field(init=False)
    reader: Any = field(default=None, repr=False)
    fingerprint_job: _FileFingerprintJob | None = field(default=None, repr=False)
    display_builder: IncrementalDisplayIndexBuilder | None = field(default=None, repr=False)
    native_staging: NativeObjStaging | None = field(default=None, repr=False)
    source_size_before: int = field(default=0, repr=False)
    source_mtime_ns_before: int = field(default=0, repr=False)
    geometry_only: bool = False
    stage_only: bool = False
    prepare_gpu: bool = True

    def __post_init__(self) -> None:
        """작업 시작 시 표시 축·정점 표시 상한·예상 fingerprint를 검증합니다."""
        try:
            self.display_transform = create_model_display_transform(
                self.axis_preset,
                self.axis_forward,
                self.axis_up,
            )
        except ValueError as error:
            raise MeshImportJobError(f"OBJ 표시 축 설정이 올바르지 않습니다: {error}") from error
        self.axis_preset = self.display_transform.preset_id
        self.axis_forward = self.display_transform.forward_axis
        self.axis_up = self.display_transform.up_axis
        try:
            self.display_vertex_limit = normalize_display_point_limit(self.display_vertex_limit)
        except ValueError as error:
            raise MeshImportJobError(f"OBJ 표시 정점 상한이 올바르지 않습니다: {error}") from error
        expected = str(self.expected_source_fingerprint).strip().lower()
        if expected and (len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected)):
            raise MeshImportJobError("OBJ source fingerprint 형식이 올바르지 않습니다.")
        self.expected_source_fingerprint = expected

    @property
    def is_terminal(self) -> bool:
        """OBJ 작업이 완료·실패·취소로 종료됐는지 반환합니다."""
        return self.stage in _MESH_TERMINAL_STAGES

    @property
    def is_running(self) -> bool:
        """OBJ 작업이 다음 TIMER 단계로 진행 가능한지 반환합니다."""
        return not self.is_terminal

    def request_cancel(self) -> bool:
        """현재 작업에 취소를 표시해 다음 안전 경계에서 롤백하게 합니다."""
        if self.is_terminal or self.cancel_requested:
            return False
        self.cancel_requested = True
        return True

    def _advance(self, stage: str, progress: float) -> None:
        """작업 단계와 단조 증가 진행률을 갱신합니다."""
        self.stage = stage
        self.progress = max(self.progress, min(1.0, max(0.0, float(progress))))

    def _cleanup_staging(self) -> None:
        """Native Mesh와 임시 배열을 해제해 실패·취소 Data Block이 남지 않게 합니다."""
        if self.reader is not None and not self.reader.done:
            self.reader.cancel()
        if self.fingerprint_job is not None:
            self.fingerprint_job.close()
        if self.display_builder is not None:
            self.display_builder.cancel()
        if self.native_staging is not None:
            self.native_staging.discard()
        self.reader = None
        self.fingerprint_job = None
        self.display_builder = None
        self.parsed_result = None
        self.display_indices = None
        self.staged_model = None
        self.native_staging = None

    def _step_fingerprint(self, *, after_read: bool, time_budget_seconds: float) -> bool:
        """구버전 복원에서만 OBJ SHA-256을 가져오기 전후 계산해 저장 지문과 비교합니다."""
        if self.fingerprint_job is None:
            self.fingerprint_job = _FileFingerprintJob(Path(self.source_path))
        self.fingerprint_job.step(time_budget_seconds)
        self.current_count = self.fingerprint_job.completed_bytes
        self.total_count = self.fingerprint_job.total_bytes
        if self.total_count > 0:
            phase_start, phase_end = ((0.05, 0.12) if not after_read else (0.70, 0.76))
            fraction = min(1.0, self.current_count / self.total_count)
            self.progress = max(self.progress, phase_start + (phase_end - phase_start) * fraction)
        if not self.fingerprint_job.done:
            return False
        fingerprint = self.fingerprint_job.result
        self.fingerprint_job.close()
        self.fingerprint_job = None
        if after_read:
            if (
                fingerprint != self.source_fingerprint_before
                or fingerprint != self.expected_source_fingerprint
            ):
                raise MeshImportJobError("가져오기 중 OBJ source 파일이 변경되었습니다. 다시 시도하십시오.")
            return True
        self.source_fingerprint_before = fingerprint
        if fingerprint != self.expected_source_fingerprint:
            raise MeshImportJobError("저장된 OBJ source fingerprint와 현재 원본이 다릅니다.")
        return True

    def commit_staging(self) -> None:
        """stage_only 호출자가 Repository 등록 성공 뒤 내장 Mesh 소유권을 확정합니다."""

        if self.native_staging is None:
            raise MeshImportJobError("확정할 OBJ native staging이 없습니다.")
        self.native_staging.commit()
        self.native_staging = None

    def discard_staging(self) -> None:
        """stage_only 호출자가 상위 transaction 실패 때 내장 후보를 폐기합니다."""

        if self.native_staging is not None:
            self.native_staging.discard()
            self.native_staging = None

    def _cancel(self) -> str:
        """취소 경로에서 GPU cache와 임시 배열을 해제하고 Runtime을 건드리지 않습니다."""
        if self.gpu_batch_prepared and self.staged_model is not None:
            _discard_gpu_batch_for_model(self.staged_model.model_id)
        self.gpu_batch_prepared = False
        self._cleanup_staging()
        self.stage = MESH_IMPORT_STAGE_CANCELLED
        self.progress = 0.0
        self.result_message = "OBJ 메시 가져오기를 취소했습니다. 기존 데이터 레이어는 유지했습니다."
        return "CANCELLED"

    def _fail(self, error: Exception) -> str:
        """실패 경로에서 GPU·임시 배열을 해제하고 사용자용 원자성 오류를 기록합니다."""
        if self.gpu_batch_prepared and self.staged_model is not None:
            _discard_gpu_batch_for_model(self.staged_model.model_id)
        self.gpu_batch_prepared = False
        self._cleanup_staging()
        self.stage = MESH_IMPORT_STAGE_FAILED
        self.error_message = (
            f"OBJ 메시를 가져오지 못했습니다. 원인: {error} "
            "영향: 데이터 레이어를 추가하지 않았습니다."
        )
        return "CANCELLED"

    def _commit(self, runtime: Any) -> None:
        """Repository·catalog 등록 실패 시 추가한 모델과 실제 준비한 GPU만 되돌립니다."""
        model = self.staged_model
        if model is None:
            raise MeshImportJobError("확정할 staged OBJ 메시가 없습니다.")
        repository = runtime.model_repository
        catalog = runtime.model_catalog
        catalog_snapshot = catalog.snapshot()
        was_empty = not catalog.models
        repository_added = False
        metadata = ImportedModelSourceMetadata(
            selected_root_absolute=self.dataset_root or self.source_path,
            sparse_path_absolute=self.source_path,
            source_format="obj",
            source_fingerprint=self.source_fingerprint_before,
            display_point_limit=self.display_vertex_limit,
            axis_preset=self.display_transform.preset_id,
            axis_forward=self.display_transform.forward_axis,
            axis_up=self.display_transform.up_axis,
            display_transform_fingerprint=self.display_transform.fingerprint,
            geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
        )
        try:
            repository.add_model(model, self.display_transform, metadata)
            repository_added = True
            catalog.add_imported_model(
                model.model_id,
                model.display_name,
                select_after_import=(not was_empty and not runtime.marker_selection.has_draft and not runtime.marker_selection.completed_pairs),
                geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
            )
            self.commit_staging()
        except Exception:
            if repository_added:
                repository.remove_model(model.model_id)
            catalog.restore_snapshot(catalog_snapshot)
            if self.gpu_batch_prepared:
                _discard_gpu_batch_for_model(model.model_id)
                self.gpu_batch_prepared = False
            self.discard_staging()
            raise

    def _complete(self) -> str:
        """준비된 모델과 원본 지문을 보존하고 읽기용 임시 참조만 해제합니다."""
        model = self.staged_model
        if model is None:
            raise MeshImportJobError("완료할 staged OBJ 메시가 없습니다.")
        self.stage = MESH_IMPORT_STAGE_COMPLETED
        self.progress = 1.0
        action = "준비했습니다" if self.stage_only else "추가했습니다"
        self.result_message = (
            f"OBJ 메시를 {action}. 정점 {model.vertex_count:,}개, "
            f"UV {model.texture_coordinate_count:,}개, "
            f"텍스처 {model.diffuse_texture_count:,}개, "
            f"원본 면 {model.original_polygon_count:,}개, "
            f"정렬용 삼각면 {model.face_count:,}개, OBJ 내장됨, "
            f"등록 정점 {model.registration_vertex_indices.size:,}개"
        )
        self.parsed_result = None
        self.reader = None
        self.fingerprint_job = None
        self.display_builder = None
        self.display_indices = None
        return "FINISHED"

    def step(self, runtime: Any | None = None, *, time_budget_seconds: float = 0.025) -> str:
        """독립 OBJ의 기존 ID는 자산·GPU 준비 전에 거부하고 연결 표면의 부모 ID는 허용합니다."""
        if self.is_terminal:
            return "FINISHED" if self.stage == MESH_IMPORT_STAGE_COMPLETED else "CANCELLED"
        if self.cancel_requested:
            return self._cancel()
        try:
            source = Path(self.source_path).expanduser()
            if self.stage == MESH_IMPORT_STAGE_FILE_CHECK:
                if source.suffix.casefold() != ".obj" or not source.is_file():
                    raise MeshImportJobError("선택한 경로가 존재하는 OBJ 파일이 아닙니다.")
                stat = source.stat()
                if stat.st_size < 1:
                    raise MeshImportJobError("OBJ 파일이 비어 있습니다.")
                self.source_size_before = int(stat.st_size)
                self.source_mtime_ns_before = int(stat.st_mtime_ns)
                self.source_path = canonical_source_path(source)
                self._advance(MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE, 0.05)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE:
                if self.expected_source_fingerprint:
                    if not self._step_fingerprint(after_read=False, time_budget_seconds=time_budget_seconds):
                        return "RUNNING"
                self._advance(MESH_IMPORT_STAGE_READ_OBJ, 0.12)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_READ_OBJ:
                if self.native_staging is None:
                    self.native_staging = import_obj_to_staging(self.source_path)
                self.parsed_result = self.native_staging.mesh
                self.current_count = len(self.parsed_result.polygons)
                self.total_count = self.current_count
                self._advance(MESH_IMPORT_STAGE_VALIDATE, 0.62)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_VALIDATE:
                mesh = self.parsed_result
                if mesh is None or len(mesh.vertices) < 1 or len(mesh.polygons) < 1:
                    raise MeshImportJobError("Blender OBJ Importer가 빈 Mesh를 반환했습니다.")
                self._advance(MESH_IMPORT_STAGE_HASH_SOURCE_AFTER, 0.70)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_HASH_SOURCE_AFTER:
                stat = Path(self.source_path).stat()
                if (
                    int(stat.st_size) != self.source_size_before
                    or int(stat.st_mtime_ns) != self.source_mtime_ns_before
                ):
                    raise MeshImportJobError("가져오기 중 OBJ source 파일이 변경되었습니다. 다시 시도하십시오.")
                if self.expected_source_fingerprint and not self._step_fingerprint(
                    after_read=True,
                    time_budget_seconds=time_budget_seconds,
                ):
                    return "RUNNING"
                self._advance(MESH_IMPORT_STAGE_PREPARE_MODEL, 0.76)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_PREPARE_MODEL:
                mesh = self.parsed_result
                if mesh is None:
                    raise MeshImportJobError("Blender OBJ Importer 결과가 없습니다.")
                model_id = str(self.model_id_factory()).strip()
                if not model_id:
                    raise MeshImportJobError("OBJ 모델 ID를 생성하지 못했습니다.")
                display_name = self.display_name_hint.strip() or Path(self.source_path).stem
                if runtime is not None and not self.stage_only:
                    if model_id in runtime.model_repository.models:
                        raise ModelRepositoryError(f"중복 OBJ 모델 ID입니다: {model_id}")
                    display_name = runtime.model_repository.unique_display_name(display_name)
                self.staged_model = commit_embedded_obj_mesh_and_build_proxy(
                    mesh,
                    model_id=model_id,
                    display_name=display_name,
                    source_path=self.source_path,
                    geometry_only=self.geometry_only,
                )
                validate_obj_limits(
                    vertex_count=self.staged_model.vertex_count,
                    triangle_count=self.staged_model.proxy_triangle_count,
                )
                self._advance(MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE, 0.82)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE:
                model = self.staged_model
                if model is None:
                    raise MeshImportJobError("표시 샘플을 만들 staged 메시가 없습니다.")
                if self.display_builder is None:
                    self.display_builder = IncrementalDisplayIndexBuilder(
                        model.registration_vertex_indices.astype(np.uint64, copy=False),
                        max_points=self.display_vertex_limit,
                        _validated_unique=True,
                    )
                sample_progress = self.display_builder.step(
                    max_records=DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE,
                    time_budget_seconds=time_budget_seconds,
                )
                self.current_count = sample_progress.processed_count
                self.total_count = sample_progress.total_count
                self.progress = max(self.progress, 0.82 + 0.06 * sample_progress.fraction)
                if not sample_progress.is_completed:
                    return "RUNNING"
                sample_offsets = self.display_builder.display_indices
                self.display_indices = np.ascontiguousarray(
                    model.registration_vertex_indices[sample_offsets],
                    dtype=np.uint32,
                )
                self.display_indices.setflags(write=False)
                self.display_builder = None
                self.staged_model = TriangleMeshModelData(
                    model_id=model.model_id,
                    display_name=model.display_name,
                    source_path=model.source_path,
                    vertices_float64=model.vertices_float64,
                    triangles_uint32=model.triangles_uint32,
                    registration_vertex_indices=model.registration_vertex_indices,
                    display_vertex_indices=self.display_indices,
                    bounds_min=model.bounds_min,
                    bounds_max=model.bounds_max,
                    geometry_revision=model.geometry_revision,
                    texcoords_float64=model.texcoords_float64,
                    texcoord_component_counts_uint8=model.texcoord_component_counts_uint8,
                    triangle_texcoord_indices_int32=model.triangle_texcoord_indices_int32,
                    materials=model.materials,
                    triangle_material_indices_int32=model.triangle_material_indices_int32,
                    triangle_polygon_indices_uint32=model.triangle_polygon_indices_uint32,
                    triangle_loop_indices_uint32=model.triangle_loop_indices_uint32,
                    embedded_asset_uuid=model.embedded_asset_uuid,
                    embedded_asset_schema_version=model.embedded_asset_schema_version,
                    embedded_data_name_hint=model.embedded_data_name_hint,
                    original_polygon_count=model.original_polygon_count,
                )
                if self.stage_only:
                    return self._complete()
                if not self.prepare_gpu:
                    self._advance(MESH_IMPORT_STAGE_COMMIT, 0.94)
                    return "RUNNING"
                self._advance(MESH_IMPORT_STAGE_PREPARE_GPU, 0.88)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_PREPARE_GPU:
                if runtime is None:
                    raise MeshImportJobError("OBJ GPU 준비에는 Runtime이 필요합니다.")
                if self.staged_model.model_id in runtime.model_repository.models:
                    raise ModelRepositoryError(f"중복 OBJ 모델 ID입니다: {self.staged_model.model_id}")
                _prepare_gpu_batch_for_model(self.staged_model.model_id, self.staged_model)  # type: ignore[union-attr]
                self.gpu_batch_prepared = True
                self._advance(MESH_IMPORT_STAGE_COMMIT, 0.94)
                return "RUNNING"
            if self.stage == MESH_IMPORT_STAGE_COMMIT:
                if runtime is None:
                    raise MeshImportJobError("OBJ Data Layer 등록에는 Runtime이 필요합니다.")
                self._commit(runtime)
                return self._complete()
            raise MeshImportJobError(f"알 수 없는 OBJ 가져오기 단계입니다: {self.stage}")
        except Exception as error:
            return self._fail(error)


def create_mesh_import_job(source_path: str | Path, **kwargs: Any) -> MeshImportJob:
    """OBJ 경로와 UI 설정으로 새 MeshImportJob을 생성합니다."""
    return MeshImportJob(source_path=str(source_path), **kwargs)


def run_mesh_import_job(runtime: Any, source_path: str | Path, **kwargs: Any) -> MeshImportJob:
    """Blender 외부 테스트에서 OBJ job을 끝까지 실행하고 결과를 반환합니다."""

    from .axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(runtime)
    if axis_reason is not None:
        raise MeshImportJobError(axis_reason)
    job = create_mesh_import_job(source_path, **kwargs)
    while not job.is_terminal:
        job.step(runtime)
    if job.stage != MESH_IMPORT_STAGE_COMPLETED:
        raise MeshImportJobError(job.error_message or job.result_message)
    return job


__all__ = (
    "MeshImportJob",
    "MeshImportJobError",
    "create_mesh_import_job",
    "mesh_import_stage_label",
    "run_mesh_import_job",
)
