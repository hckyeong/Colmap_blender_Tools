"""COLMAP sparse model을 단계별로 준비하고 Runtime에 원자 등록합니다."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal

from .display_axes import (
    DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS,
    DEFAULT_UP_AXIS,
    ModelDisplayTransform,
    axis_summary,
    create_model_display_transform,
)
from .logging_utils import get_logger
from .model_repository import (
    ColmapModelData,
    DuplicateModelSourceError,
    ImportedModelSourceMetadata,
    ModelRepositoryError,
    canonical_source_path,
    create_model_id,
    model_camera_count,
    model_image_count,
    model_point_count,
)
from .viewport.display_sampling import (
    DEFAULT_DISPLAY_POINT_LIMIT,
    DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE,
    DISPLAY_SAMPLE_POLICY,
    IncrementalDisplayIndexBuilder,
    normalize_display_point_limit,
)


_LOGGER = get_logger("importer")

IMPORT_READ_MAX_RECORDS = None
IMPORT_READ_TIME_BUDGET_SECONDS = 0.025
IMPORT_FINGERPRINT_TIME_BUDGET_SECONDS = 0.025
# COLMAP Importer와 같은 25ms 예산 안에서 레코드 수 제한 없이 처리합니다.
IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = min(DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE, 100_000)
IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS = 0.025

IMPORT_STAGE_FILE_CHECK = "FILE_CHECK"
IMPORT_STAGE_HASH_SOURCE_BEFORE = "HASH_SOURCE_BEFORE"
IMPORT_STAGE_READ_MODEL = "READ_MODEL"
IMPORT_STAGE_VALIDATE_MODEL = "VALIDATE_MODEL"
IMPORT_STAGE_HASH_SOURCE_AFTER = "HASH_SOURCE_AFTER"
IMPORT_STAGE_PREPARE_MODEL = "PREPARE_MODEL"
IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE = "PREPARE_DISPLAY_SAMPLE"
IMPORT_STAGE_PREPARE_GPU = "PREPARE_GPU"
IMPORT_STAGE_COMMIT = "COMMIT"
IMPORT_STAGE_COMPLETED = "COMPLETED"
IMPORT_STAGE_CANCELLED = "CANCELLED"
IMPORT_STAGE_FAILED = "FAILED"

_TERMINAL_STAGES = frozenset(
    {IMPORT_STAGE_COMPLETED, IMPORT_STAGE_CANCELLED, IMPORT_STAGE_FAILED}
)
_READ_STAGE_PROGRESS_RANGES = {
    "RIGS": (0.18, 0.22),
    "CAMERAS": (0.22, 0.30),
    "FRAMES": (0.30, 0.34),
    "IMAGES": (0.34, 0.48),
    "POINTS3D": (0.48, 0.65),
    "COMPLETED": (0.65, 0.65),
}
ImportStepResult = Literal["RUNNING", "FINISHED", "CANCELLED"]


class ImportJobError(RuntimeError):
    """가져오기 작업의 단계·원자 등록 경계에서 발생한 오류입니다."""


def _source_path_text(source_path: str | Path) -> str:
    """UI와 로그에 표시할 선택 sparse model 경로를 정규 문자열로 반환합니다."""
    raw_path = str(source_path).strip()
    if not raw_path:
        return ""
    return str(Path(raw_path).expanduser())


def import_stage_label(stage: str) -> str:
    """Runtime 단계 식별자를 UI에 표시할 짧은 한국어 문구로 변환합니다."""
    labels = {
        IMPORT_STAGE_FILE_CHECK: "파일 검사",
        IMPORT_STAGE_HASH_SOURCE_BEFORE: "가져오기 전 원본 확인",
        IMPORT_STAGE_READ_MODEL: "COLMAP 데이터 읽기",
        IMPORT_STAGE_VALIDATE_MODEL: "참조 무결성 검사",
        IMPORT_STAGE_HASH_SOURCE_AFTER: "가져오기 후 원본 재확인",
        IMPORT_STAGE_PREPARE_MODEL: "원본 점 테이블 준비",
        IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE: "뷰포트 표시 점 선택",
        IMPORT_STAGE_PREPARE_GPU: "GPU 배치 준비",
        IMPORT_STAGE_COMMIT: "Data Layers 등록",
        IMPORT_STAGE_COMPLETED: "가져오기 완료",
        IMPORT_STAGE_CANCELLED: "가져오기 취소",
        IMPORT_STAGE_FAILED: "가져오기 실패",
    }
    return labels.get(stage, "알 수 없는 가져오기 단계")


def _point_count_from_parsed(model: Any) -> int:
    """I/O 모델에서 진행 표시용 희소점 개수를 안전하게 읽습니다."""
    points = getattr(model, "points", None)
    point_ids = getattr(points, "point_ids", None)
    return len(point_ids) if point_ids is not None else 0


def _failure_message(reason: Exception | str) -> str:
    """원인·영향·사용자 조치를 포함하는 실패 상태 문구를 구성합니다."""
    return (
        "COLMAP 모델을 가져오지 못했습니다. "
        f"원인: {reason} "
        "영향: 데이터 레이어를 추가하지 않았습니다. "
        "조치: 원본 sparse model의 파일 세트와 참조 무결성을 확인하십시오."
    )


def _reader_failure_reason(reader: Any) -> Exception:
    """증분 reader가 예외 없이 실패 상태를 반환했을 때 원인을 보존합니다."""
    error = getattr(reader, "error", None)
    if isinstance(error, Exception):
        return error
    return ImportJobError("증분 COLMAP reader가 원인 정보 없이 실패했습니다.")


def _prepare_gpu_batch_for_model(model_id: str, model_data: ColmapModelData) -> None:
    """렌더러 공개 경계로 한 모델의 GPU 배치를 준비하고 실패를 전달합니다."""
    from .viewport import renderer

    prepare = getattr(renderer, "prepare_gpu_batch_for_model", None)
    if callable(prepare):
        prepare(model_id, model_data)
        return
    ensure = getattr(renderer, "ensure_gpu_batches", None)
    if not callable(ensure):
        raise ImportJobError("GPU 배치 준비 함수를 찾을 수 없습니다.")
    ensure()


def _discard_gpu_batch_for_model(model_id: str) -> None:
    """원자 등록 롤백에서 새 모델의 GPU 배치만 안전하게 폐기합니다."""
    from .viewport import renderer

    discard = getattr(renderer, "discard_gpu_batch_for_model", None)
    if callable(discard):
        discard(model_id)


@dataclass
class ImportJob:
    """Blender TIMER가 한 단계씩 처리할 COLMAP 모델 가져오기 상태입니다."""

    source_path: str
    model_id_factory: Callable[[], str] = create_model_id
    input_format: str = "AUTO"
    axis_preset: str = DEFAULT_AXIS_PRESET
    axis_forward: str = DEFAULT_FORWARD_AXIS
    axis_up: str = DEFAULT_UP_AXIS
    display_point_limit: int = DEFAULT_DISPLAY_POINT_LIMIT
    display_transform: ModelDisplayTransform = field(
        default_factory=lambda: create_model_display_transform(
            DEFAULT_AXIS_PRESET,
            DEFAULT_FORWARD_AXIS,
            DEFAULT_UP_AXIS,
        )
    )
    dataset_root: str = ""
    expected_source_fingerprint: str = ""
    selected_format: str = ""
    stage: str = IMPORT_STAGE_FILE_CHECK
    progress: float = 0.0
    current_count: int = 0
    total_count: int = 0
    reader_stage: str = ""
    cancel_requested: bool = False
    display_name_hint: str = ""
    staged_model: ColmapModelData | None = None
    error_message: str = ""
    result_message: str = ""
    layout: Any = field(default=None, repr=False)
    reader: Any = field(default=None, repr=False)
    fingerprint_job: Any = field(default=None, repr=False)
    source_fingerprint_before: str = field(default="", repr=False)
    parsed_model: Any = field(default=None, repr=False)
    display_index_builder: IncrementalDisplayIndexBuilder | None = field(
        default=None,
        repr=False,
    )
    display_sample_started_at: float | None = field(default=None, repr=False)
    gpu_batch_prepared: bool = field(default=False, repr=False)
    validation_performed: bool = field(default=False, repr=False)
    started_at: float = field(default_factory=perf_counter, repr=False)

    def __post_init__(self) -> None:
        """작업 축·표시 점 상한 snapshot을 초기 경계에서 검증합니다."""
        try:
            expected_transform = create_model_display_transform(
                self.axis_preset,
                self.axis_forward,
                self.axis_up,
            )
        except ValueError as error:
            raise ImportJobError(
                f"가져오기 작업의 표시 축 설정이 올바르지 않습니다: {error}"
            ) from error
        if not isinstance(self.display_transform, ModelDisplayTransform):
            raise ImportJobError("가져오기 작업의 표시 축 변환 형식이 올바르지 않습니다.")
        if self.display_transform.fingerprint != expected_transform.fingerprint:
            raise ImportJobError("가져오기 작업의 표시 축 snapshot이 서로 일치하지 않습니다.")
        self.axis_preset = expected_transform.preset_id
        self.axis_forward = expected_transform.forward_axis
        self.axis_up = expected_transform.up_axis
        self.display_point_limit = normalize_display_point_limit(self.display_point_limit)
        expected_fingerprint = str(self.expected_source_fingerprint).strip().lower()
        if expected_fingerprint and (
            len(expected_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in expected_fingerprint)
        ):
            raise ImportJobError("복원할 COLMAP source fingerprint 형식이 올바르지 않습니다.")
        self.expected_source_fingerprint = expected_fingerprint

    @property
    def is_terminal(self) -> bool:
        """완료·취소·실패로 더 진행할 단계가 없는지 반환합니다."""
        return self.stage in _TERMINAL_STAGES

    @property
    def is_running(self) -> bool:
        """현재 TIMER가 다음 단계로 진행할 수 있는지 반환합니다."""
        return not self.is_terminal

    def request_cancel(self) -> bool:
        """다음 단계 시작 전에 처리할 취소 요청을 한 번 기록합니다."""
        if self.is_terminal or self.cancel_requested:
            return False
        self.cancel_requested = True
        _LOGGER.info(
            "event=import_cancel_requested source=%s stage=%s axis=%s",
            self.source_path,
            self.stage,
            self.display_transform.fingerprint,
        )
        return True

    def _advance(self, stage: str, progress: float) -> None:
        """현재 단계와 0~1 진행률을 함께 갱신하고 구조화 로그를 남깁니다."""
        self.stage = stage
        if stage != IMPORT_STAGE_READ_MODEL:
            self.reader_stage = ""
        self.progress = max(0.0, min(float(progress), 1.0))
        _LOGGER.info(
            "event=import_stage_changed source=%s stage=%s progress=%.3f axis=%s",
            self.source_path,
            stage,
            self.progress,
            self.display_transform.fingerprint,
        )

    def _update_read_progress(self, progress: Any) -> None:
        """증분 reader의 부분 카운터를 단조 증가 UI 진행률로 변환합니다."""
        current_count = max(0, int(getattr(progress, "current_count", 0)))
        total_value = getattr(progress, "total_count", None)
        total_count = max(0, int(total_value)) if total_value is not None else 0
        if total_count and current_count > total_count:
            raise ImportJobError("증분 COLMAP reader 진행 카운터가 전체 레코드 수를 초과했습니다.")
        self.current_count = current_count
        self.total_count = total_count
        reader_stage = str(getattr(progress, "stage", ""))
        self.reader_stage = reader_stage
        start, end = _READ_STAGE_PROGRESS_RANGES.get(reader_stage, (0.12, 0.70))
        fraction_value = getattr(progress, "fraction", None)
        fraction = 0.0 if fraction_value is None else float(fraction_value)
        fraction = max(0.0, min(fraction, 1.0))
        self.progress = max(self.progress, start + ((end - start) * fraction))

    def _release_reader(self) -> None:
        """열려 있을 수 있는 증분 reader를 취소해 파일 핸들과 참조를 정리합니다."""
        reader = self.reader
        self.reader = None
        if reader is None:
            return
        try:
            reader.cancel()
        except Exception as error:
            _LOGGER.warning(
                "event=incremental_reader_release_failed source=%s error_type=%s reason=%s",
                self.source_path,
                type(error).__name__,
                error,
            )

    def _release_fingerprint_job(self) -> None:
        """열려 있을 수 있는 source 지문 파일 핸들을 정리합니다."""
        fingerprint_job = self.fingerprint_job
        self.fingerprint_job = None
        if fingerprint_job is None:
            return
        try:
            fingerprint_job.close()
        except Exception as error:
            _LOGGER.warning(
                "event=source_fingerprint_release_failed source=%s error_type=%s reason=%s",
                self.source_path,
                type(error).__name__,
                error,
            )

    def _release_display_index_builder(self) -> None:
        """진행 중 표시 샘플 후보를 취소하고 대형 배열 참조를 해제합니다."""
        builder = self.display_index_builder
        self.display_index_builder = None
        if builder is None:
            return
        builder.cancel()

    def _step_fingerprint(self, *, after_read: bool) -> bool:
        """source 지문을 한 tick 진행하고 완료 시 가져오기 전후 일치를 검사합니다."""
        from .colmap_io import SourceFingerprintJob

        if self.layout is None:
            raise ImportJobError("원본 지문을 계산할 COLMAP layout이 없습니다.")
        if self.fingerprint_job is None:
            self.fingerprint_job = SourceFingerprintJob(self.layout)
        self.fingerprint_job.step(IMPORT_FINGERPRINT_TIME_BUDGET_SECONDS)
        self.current_count = self.fingerprint_job.completed_bytes
        self.total_count = self.fingerprint_job.total_bytes
        start, end = ((0.72, 0.82) if after_read else (0.05, 0.18))
        fraction = (
            self.current_count / self.total_count if self.total_count else 1.0
        )
        self.progress = max(self.progress, start + ((end - start) * fraction))
        if not self.fingerprint_job.done:
            return False
        fingerprint = self.fingerprint_job.result
        self._release_fingerprint_job()
        if after_read:
            if fingerprint != self.source_fingerprint_before:
                raise ImportJobError(
                    "가져오기 중 COLMAP source 파일이 변경되었습니다. 다시 시도하십시오."
                )
        else:
            self.source_fingerprint_before = fingerprint
            if (
                self.expected_source_fingerprint
                and fingerprint != self.expected_source_fingerprint
            ):
                raise ImportJobError(
                    "저장된 COLMAP source fingerprint와 현재 원본이 다릅니다. "
                    "저장 당시 원본을 다시 연결하십시오."
                )
        return True

    def _cancel(self) -> ImportStepResult:
        """임시 참조를 해제하고 기존 Runtime을 건드리지 않은 채 작업을 취소합니다."""
        self._release_reader()
        self._release_fingerprint_job()
        if self.display_index_builder is not None:
            _LOGGER.info(
                "event=display_sample_cancelled source=%s processed_count=%s "
                "total_count=%s requested_limit=%s policy=%s",
                self.source_path,
                self.current_count,
                self.total_count,
                self.display_point_limit,
                DISPLAY_SAMPLE_POLICY,
            )
        self._release_display_index_builder()
        if self.gpu_batch_prepared and self.staged_model is not None:
            _discard_gpu_batch_for_model(self.staged_model.model_id)
        self.gpu_batch_prepared = False
        self.layout = None
        self.parsed_model = None
        self.staged_model = None
        self.stage = IMPORT_STAGE_CANCELLED
        self.progress = 0.0
        self.result_message = "COLMAP 모델 가져오기를 취소했습니다. 기존 데이터 레이어는 유지했습니다."
        _LOGGER.info(
            "event=import_cancelled source=%s axis=%s",
            self.source_path,
            self.display_transform.fingerprint,
        )
        return "CANCELLED"

    def _fail(self, error: Exception, *, expected: bool) -> ImportStepResult:
        """임시 참조를 해제하고 원자성 안내가 포함된 실패 상태를 기록합니다."""
        self._release_reader()
        self._release_fingerprint_job()
        if self.display_index_builder is not None:
            _LOGGER.warning(
                "event=display_sample_failed source=%s processed_count=%s total_count=%s "
                "requested_limit=%s policy=%s error_type=%s",
                self.source_path,
                self.current_count,
                self.total_count,
                self.display_point_limit,
                DISPLAY_SAMPLE_POLICY,
                type(error).__name__,
            )
        self._release_display_index_builder()
        if self.gpu_batch_prepared and self.staged_model is not None:
            _discard_gpu_batch_for_model(self.staged_model.model_id)
        self.gpu_batch_prepared = False
        self.layout = None
        self.parsed_model = None
        self.staged_model = None
        self.stage = IMPORT_STAGE_FAILED
        self.error_message = _failure_message(error)
        if expected:
            _LOGGER.warning(
                "event=import_failed expected=true source=%s axis=%s error_type=%s reason=%s",
                self.source_path,
                self.display_transform.fingerprint,
                type(error).__name__,
                error,
            )
        else:
            _LOGGER.exception(
                "event=import_failed expected=false source=%s axis=%s error_type=%s reason=%s",
                self.source_path,
                self.display_transform.fingerprint,
                type(error).__name__,
                error,
            )
        return "CANCELLED"

    def _commit(self, runtime: Any) -> None:
        """저장소·GPU·Data Layers를 실패 시 이전 스냅샷으로 되돌리며 함께 확정합니다."""
        model = self.staged_model
        if model is None:
            raise ImportJobError("확정할 staged COLMAP 모델이 없습니다.")
        repository = runtime.model_repository
        catalog = runtime.model_catalog
        catalog_snapshot = catalog.snapshot()
        was_empty = not catalog.models
        repository_added = False
        source_metadata = ImportedModelSourceMetadata(
            selected_root_absolute=self.dataset_root or self.source_path,
            sparse_path_absolute=self.source_path,
            source_format=self.selected_format,
            source_fingerprint=self.source_fingerprint_before,
            display_point_limit=self.display_point_limit,
            axis_preset=self.display_transform.preset_id,
            axis_forward=self.display_transform.forward_axis,
            axis_up=self.display_transform.up_axis,
            display_transform_fingerprint=self.display_transform.fingerprint,
        )
        try:
            repository.add_model(model, self.display_transform, source_metadata)
            repository_added = True
            catalog.add_imported_model(
                model.model_id,
                model.display_name,
                select_after_import=(
                    not was_empty
                    and not runtime.marker_selection.has_draft
                    and not runtime.marker_selection.completed_pairs
                ),
            )
        except Exception:
            _discard_gpu_batch_for_model(model.model_id)
            self.gpu_batch_prepared = False
            if repository_added:
                repository.remove_model(model.model_id)
            catalog.restore_snapshot(catalog_snapshot)
            raise

    def _complete(self) -> ImportStepResult:
        """성공한 모델의 요약 문구와 종료 상태를 기록합니다."""
        model = self.staged_model
        if model is None:
            raise ImportJobError("완료할 staged COLMAP 모델이 없습니다.")
        source_count = model_point_count(model)
        display_count = int(model.points.display_indices.size)
        self.stage = IMPORT_STAGE_COMPLETED
        self.progress = 1.0
        self.current_count = source_count
        self.total_count = self.current_count
        self._release_reader()
        self.result_message = (
            f"{model.display_name} 가져오기 완료 · 점 {source_count:,} · "
            f"표시 점 {display_count:,} / {source_count:,} · "
            f"카메라 {model_camera_count(model):,} · 이미지 {model_image_count(model):,} · "
            f"표시 축 {axis_summary(self.display_transform)}"
        )
        elapsed = perf_counter() - self.started_at
        _LOGGER.info(
            "event=import_completed model_id=%s points=%s display_count=%s "
            "display_point_limit=%s sampling_policy=%s elapsed_seconds=%.3f "
            "axis_preset=%s forward=%s up=%s axis=%s",
            model.model_id,
            source_count,
            display_count,
            self.display_point_limit,
            DISPLAY_SAMPLE_POLICY,
            elapsed,
            self.display_transform.preset_id,
            self.display_transform.forward_axis,
            self.display_transform.up_axis,
            self.display_transform.fingerprint,
        )
        return "FINISHED"

    def step(self, runtime: Any) -> ImportStepResult:
        """한 TIMER tick에서 한 가져오기 단계만 실행하고 다음 호출 시점을 반환합니다."""
        if self.is_terminal:
            return "FINISHED" if self.stage == IMPORT_STAGE_COMPLETED else "CANCELLED"
        if self.cancel_requested:
            return self._cancel()
        try:
            if self.stage == IMPORT_STAGE_FILE_CHECK:
                from .colmap_io import discover_sparse_model_with_dataset_root

                selected_root = self.source_path
                inferred_dataset_root, self.layout = (
                    discover_sparse_model_with_dataset_root(
                        self.source_path,
                        input_format=self.input_format,
                    )
                )
                if not self.dataset_root or canonical_source_path(
                    self.dataset_root
                ) == canonical_source_path(selected_root):
                    self.dataset_root = str(inferred_dataset_root)
                resolved_source_path = str(self.layout.source_path)
                canonical_path = canonical_source_path(resolved_source_path)
                if runtime.model_repository.contains_source_path(canonical_path):
                    raise DuplicateModelSourceError(
                        f"같은 COLMAP 모델 폴더를 이미 불러왔습니다: {canonical_path}"
                    )
                if canonical_source_path(selected_root) != canonical_path:
                    self.display_name_hint = (
                        Path(selected_root).expanduser().resolve(strict=False).name
                    )
                    _LOGGER.info(
                        "event=import_source_resolved selected_root=%s source=%s",
                        selected_root,
                        resolved_source_path,
                    )
                self.source_path = resolved_source_path
                self.selected_format = self.layout.selected_format
                self._advance(IMPORT_STAGE_HASH_SOURCE_BEFORE, 0.05)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_HASH_SOURCE_BEFORE:
                from .colmap_io import create_incremental_sparse_model_reader

                if not self._step_fingerprint(after_read=False):
                    return "RUNNING"
                self.reader = create_incremental_sparse_model_reader(self.layout)
                self._advance(IMPORT_STAGE_READ_MODEL, 0.18)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_READ_MODEL:
                if self.reader is None:
                    raise ImportJobError("읽을 증분 COLMAP reader가 없습니다.")
                read_progress = self.reader.step(
                    max_records=IMPORT_READ_MAX_RECORDS,
                    time_budget_seconds=IMPORT_READ_TIME_BUDGET_SECONDS,
                )
                self._update_read_progress(read_progress)
                if read_progress.is_cancelled:
                    self.cancel_requested = True
                    return self._cancel()
                if read_progress.is_failed:
                    raise _reader_failure_reason(self.reader)
                if not read_progress.is_completed:
                    return "RUNNING"
                self.parsed_model = self.reader.model
                self._release_reader()
                self.total_count = _point_count_from_parsed(self.parsed_model)
                self.current_count = self.total_count
                self._advance(IMPORT_STAGE_VALIDATE_MODEL, 0.65)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_VALIDATE_MODEL:
                from .colmap_io import validate_model

                if self.validation_performed:
                    raise ImportJobError("COLMAP 참조 무결성 검증을 두 번 실행할 수 없습니다.")
                validate_model(self.parsed_model)
                self.validation_performed = True
                self._advance(IMPORT_STAGE_HASH_SOURCE_AFTER, 0.72)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_HASH_SOURCE_AFTER:
                if not self._step_fingerprint(after_read=True):
                    return "RUNNING"
                self._advance(IMPORT_STAGE_PREPARE_MODEL, 0.82)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_PREPARE_MODEL:
                if not self.validation_performed:
                    raise ImportJobError(
                        "표시 샘플링 전에 COLMAP POINT3D_ID 검증이 완료되어야 합니다."
                    )
                self.staged_model = runtime.model_repository.prepare_model_data(
                    self.parsed_model,
                    self.source_path,
                    model_id_factory=self.model_id_factory,
                    display_name_hint=self.display_name_hint,
                )
                self.display_index_builder = IncrementalDisplayIndexBuilder(
                    self.staged_model.points.point_ids,
                    max_points=self.display_point_limit,
                    chunk_size=IMPORT_DISPLAY_SAMPLE_MAX_RECORDS,
                    _validated_unique=True,
                )
                self.display_sample_started_at = perf_counter()
                self.current_count = 0
                self.total_count = int(self.staged_model.points.count)
                _LOGGER.info(
                    "event=display_sample_started model_id=%s source_count=%s "
                    "requested_limit=%s policy=%s",
                    self.staged_model.model_id,
                    self.total_count,
                    self.display_point_limit,
                    DISPLAY_SAMPLE_POLICY,
                )
                self._advance(IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE, 0.84)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE:
                builder = self.display_index_builder
                if builder is None or self.staged_model is None:
                    raise ImportJobError("표시 샘플을 준비할 staged COLMAP 모델이 없습니다.")
                sample_progress = builder.step(
                    max_records=IMPORT_DISPLAY_SAMPLE_MAX_RECORDS,
                    time_budget_seconds=IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS,
                )
                self.current_count = sample_progress.processed_count
                self.total_count = sample_progress.total_count
                self.progress = max(self.progress, 0.84 + (0.06 * sample_progress.fraction))
                if sample_progress.is_cancelled:
                    self.cancel_requested = True
                    return self._cancel()
                if not sample_progress.is_completed:
                    return "RUNNING"
                display_indices = builder.display_indices
                try:
                    self.staged_model = replace(
                        self.staged_model,
                        points=replace(
                            self.staged_model.points,
                            display_indices=display_indices,
                        ),
                    )
                except TypeError as error:
                    raise ImportJobError(
                        "불변 COLMAP 모델의 표시 인덱스를 교체할 수 없습니다."
                    ) from error
                sample_started_at = self.display_sample_started_at
                elapsed = (
                    perf_counter() - sample_started_at
                    if sample_started_at is not None
                    else 0.0
                )
                _LOGGER.info(
                    "event=display_sample_completed model_id=%s source_count=%s "
                    "display_count=%s requested_limit=%s elapsed_seconds=%.3f policy=%s",
                    self.staged_model.model_id,
                    self.total_count,
                    int(display_indices.size),
                    self.display_point_limit,
                    elapsed,
                    DISPLAY_SAMPLE_POLICY,
                )
                self.display_index_builder = None
                self._advance(IMPORT_STAGE_PREPARE_GPU, 0.90)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_PREPARE_GPU:
                if self.staged_model is None:
                    raise ImportJobError("GPU 배치를 준비할 staged COLMAP 모델이 없습니다.")
                _prepare_gpu_batch_for_model(
                    self.staged_model.model_id,
                    self.staged_model,
                )
                self.gpu_batch_prepared = True
                self._advance(IMPORT_STAGE_COMMIT, 0.96)
                return "RUNNING"
            if self.stage == IMPORT_STAGE_COMMIT:
                self._commit(runtime)
                return self._complete()
            raise ImportJobError(f"알 수 없는 COLMAP 가져오기 단계입니다: {self.stage}")
        except (ModelRepositoryError, ImportJobError, ValueError, OSError) as error:
            return self._fail(error, expected=True)
        except Exception as error:
            return self._fail(error, expected=False)


def create_import_job(
    source_path: str | Path,
    *,
    input_format: str = "AUTO",
    axis_preset: str = DEFAULT_AXIS_PRESET,
    axis_forward: str = DEFAULT_FORWARD_AXIS,
    axis_up: str = DEFAULT_UP_AXIS,
    display_point_limit: int = DEFAULT_DISPLAY_POINT_LIMIT,
    model_id_factory: Callable[[], str] = create_model_id,
    display_name_hint: str = "",
    expected_source_fingerprint: str = "",
    dataset_root: str | Path | None = None,
) -> ImportJob:
    """선택한 sparse model·축·표시 점 상한 snapshot으로 작업을 생성합니다."""
    source_text = _source_path_text(source_path)
    if not source_text:
        raise ImportJobError("COLMAP sparse model 폴더 경로가 비어 있습니다.")
    requested_format = str(input_format).upper()
    if requested_format not in {"AUTO", "BINARY", "TEXT"}:
        raise ImportJobError(f"지원하지 않는 COLMAP 입력 형식입니다: {input_format}")
    try:
        display_transform = create_model_display_transform(
            axis_preset,
            axis_forward,
            axis_up,
        )
    except ValueError as error:
        raise ImportJobError(f"표시 축 설정이 올바르지 않습니다: {error}") from error
    try:
        return ImportJob(
            source_path=source_text,
            model_id_factory=model_id_factory,
            input_format=requested_format,
            axis_preset=display_transform.preset_id,
            axis_forward=display_transform.forward_axis,
            axis_up=display_transform.up_axis,
            display_point_limit=display_point_limit,
            display_transform=display_transform,
            dataset_root=(
                _source_path_text(dataset_root)
                if dataset_root is not None
                else source_text
            ),
            display_name_hint=str(display_name_hint).strip(),
            expected_source_fingerprint=expected_source_fingerprint,
        )
    except ValueError as error:
        raise ImportJobError(f"표시 점 상한이 올바르지 않습니다: {error}") from error


def begin_import_job(
    runtime: Any,
    source_path: str | Path,
    *,
    input_format: str = "AUTO",
    axis_preset: str = DEFAULT_AXIS_PRESET,
    axis_forward: str = DEFAULT_FORWARD_AXIS,
    axis_up: str = DEFAULT_UP_AXIS,
    display_point_limit: int = DEFAULT_DISPLAY_POINT_LIMIT,
) -> ImportJob:
    """마커 모달과 중첩되지 않는 단일 Runtime 표시 점 snapshot 작업을 시작합니다."""
    from .project_persistence.runtime import restore_input_block_reason

    restore_reason = restore_input_block_reason(runtime)
    if restore_reason is not None:
        raise ImportJobError(restore_reason)
    if runtime.import_in_progress:
        raise ImportJobError("이미 COLMAP 모델 가져오기가 진행 중입니다.")
    if runtime.modal_active:
        raise ImportJobError("진행 중인 마커 입력을 끝낸 뒤 COLMAP 모델을 불러오십시오.")
    job = create_import_job(
        source_path,
        input_format=input_format,
        axis_preset=axis_preset,
        axis_forward=axis_forward,
        axis_up=axis_up,
        display_point_limit=display_point_limit,
    )
    runtime.import_job = job
    _LOGGER.info(
        "event=import_started source=%s display_point_limit=%s axis_preset=%s "
        "forward=%s up=%s axis=%s",
        job.source_path,
        job.display_point_limit,
        job.display_transform.preset_id,
        job.display_transform.forward_axis,
        job.display_transform.up_axis,
        job.display_transform.fingerprint,
    )
    return job


def cancel_import_job(runtime: Any) -> bool:
    """진행 중인 단일 가져오기 작업에 취소를 요청합니다."""
    job = runtime.import_job
    return bool(job is not None and job.request_cancel())
