"""저장된 동일 source를 이전 검증 결과로 신뢰해 빠르게 복원합니다."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import MappingProxyType

from .fingerprint import (
    SourceFileSignature,
    SourceFingerprintJob,
    source_fingerprint,
    verify_source_signatures,
)
from .import_job import SCHEMA_VERSION, freeze_validated_reconstruction
from .layout import resolve_layout, sparse_root_for
from .model import (
    ImagePathManifest,
    LoadedReconstruction,
    Reconstruction,
    ResolvedLayout,
    SourceDescriptor,
    ValidatedImagePath,
    ValidatedReconstructionToken,
)
from .paths import ImagePathValidationJob, validate_image_files
from .reconstruction_read_job import ReconstructionReadJob


SOURCE_CHANGED_REIMPORT_MESSAGE = (
    "원본 COLMAP 파일이 마지막 Import 또는 Export 이후 변경되었습니다. "
    "기존 편집 기록은 적용하지 않았습니다. COLMAP 데이터를 다시 Import하십시오."
)


class RuntimeSourceRestorePhase(Enum):
    """동일 source 빠른 복원의 재진입 가능한 Core 단계를 정의합니다."""

    CREATED = auto()
    VERIFY_SOURCE = auto()
    READ_MODEL = auto()
    VALIDATE_IMAGES = auto()
    BUILD_RESULT = auto()
    DONE = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class RuntimeSourceRestorePlan:
    """저장된 source 권위값과 복원할 정확한 경로를 불변 입력으로 고정합니다."""

    dataset_root: Path | str
    model_directory: Path | str
    images_directory: Path | str
    input_format: str
    expected_dataset_uuid: str
    expected_source_fingerprint: str
    expected_schema_version: int

    def __post_init__(self) -> None:
        """경로·형식·신뢰 token을 복원 시작 전에 정규화하고 검증합니다."""

        dataset_root = Path(self.dataset_root).expanduser().resolve()
        model_directory = Path(self.model_directory).expanduser().resolve()
        images_directory = Path(self.images_directory).expanduser().resolve()
        input_format = str(self.input_format).upper()
        dataset_uuid = str(self.expected_dataset_uuid or "")
        fingerprint = str(self.expected_source_fingerprint or "")
        if input_format not in {"BINARY", "TEXT"}:
            raise ValueError(
                "저장된 COLMAP 입력 형식이 현재 신뢰 기준과 다릅니다. "
                "COLMAP 데이터를 다시 Import하십시오."
            )
        if not dataset_uuid:
            raise ValueError(
                "저장된 Dataset UUID가 없습니다. COLMAP 데이터를 다시 Import하십시오."
            )
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError(
                "저장된 COLMAP source fingerprint가 없습니다. "
                "COLMAP 데이터를 다시 Import하십시오."
            )
        if (
            isinstance(self.expected_schema_version, bool)
            or not isinstance(self.expected_schema_version, int)
        ):
            raise ValueError(
                "저장된 COLMAP Source Schema가 올바르지 않습니다. "
                "COLMAP 데이터를 다시 Import하십시오."
            )
        object.__setattr__(self, "dataset_root", dataset_root)
        object.__setattr__(self, "model_directory", model_directory)
        object.__setattr__(self, "images_directory", images_directory)
        object.__setattr__(self, "input_format", input_format)
        object.__setattr__(self, "expected_dataset_uuid", dataset_uuid)
        object.__setattr__(self, "expected_source_fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class RuntimeSourceRestoreProgress:
    """한 시점의 빠른 source 복원 진행 상태를 불변 값으로 전달합니다."""

    phase: RuntimeSourceRestorePhase
    overall_fraction: float
    completed_units: int
    total_units: int
    message: str


_PHASE_RANGES: dict[RuntimeSourceRestorePhase, tuple[float, float]] = {
    RuntimeSourceRestorePhase.CREATED: (0.0, 0.0),
    RuntimeSourceRestorePhase.VERIFY_SOURCE: (0.0, 0.22),
    RuntimeSourceRestorePhase.READ_MODEL: (0.22, 0.82),
    RuntimeSourceRestorePhase.VALIDATE_IMAGES: (0.82, 0.96),
    RuntimeSourceRestorePhase.BUILD_RESULT: (0.96, 1.0),
    RuntimeSourceRestorePhase.DONE: (1.0, 1.0),
    RuntimeSourceRestorePhase.FAILED: (0.0, 1.0),
}

_PHASE_MESSAGES: dict[RuntimeSourceRestorePhase, str] = {
    RuntimeSourceRestorePhase.CREATED: "저장된 원본 확인 준비",
    RuntimeSourceRestorePhase.VERIFY_SOURCE: "저장된 원본 확인 중",
    RuntimeSourceRestorePhase.READ_MODEL: "COLMAP 데이터 읽기 중",
    RuntimeSourceRestorePhase.VALIDATE_IMAGES: "이미지 경로 확인 중",
    RuntimeSourceRestorePhase.BUILD_RESULT: "복원 결과 준비 중",
    RuntimeSourceRestorePhase.DONE: "COLMAP 원본 복원 완료",
    RuntimeSourceRestorePhase.FAILED: "COLMAP 원본 복원 실패",
}


class RuntimeSourceRestoreJob:
    """동일 fingerprint·현재 schema의 저장 source를 전체 검증 없이 복원합니다."""

    def __init__(self, plan: RuntimeSourceRestorePlan) -> None:
        """저장 신뢰 token을 검사하고 증분 해시·Parser 자원을 준비합니다."""

        if not isinstance(plan, RuntimeSourceRestorePlan):
            raise ValueError("유효한 RuntimeSourceRestorePlan이 필요합니다.")
        if plan.expected_schema_version != SCHEMA_VERSION:
            raise ValueError(
                "저장된 COLMAP Source Schema가 현재 신뢰 기준과 다릅니다. "
                "자동 변환하지 않았습니다. COLMAP 데이터를 다시 Import하십시오."
            )
        if not Path(plan.dataset_root).is_dir():
            raise ValueError(f"Dataset Root가 존재하지 않습니다: {plan.dataset_root}")
        if not Path(plan.images_directory).is_dir():
            raise ValueError(
                f"Images Directory가 존재하지 않습니다: {plan.images_directory}"
            )
        self._plan = plan
        self._layout = resolve_layout(plan.model_directory, plan.input_format)
        self._phase = RuntimeSourceRestorePhase.CREATED
        self._result: LoadedReconstruction | None = None
        self._error: Exception | None = None
        self._fingerprint_job: SourceFingerprintJob | None = None
        self._read_job: ReconstructionReadJob | None = None
        self._image_job: ImagePathValidationJob | None = None
        self._signatures: tuple[SourceFileSignature, ...] = ()
        self._model: Reconstruction | None = None
        self._image_records = ()
        self._resolved_image_paths: tuple[Path, ...] = ()
        self._progress = self._snapshot(
            RuntimeSourceRestorePhase.CREATED,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
        )

    @property
    def plan(self) -> RuntimeSourceRestorePlan:
        """복원 시작 시 고정한 source plan을 반환합니다."""

        return self._plan

    @property
    def done(self) -> bool:
        """빠른 source 복원이 성공적으로 끝났는지 반환합니다."""

        return self._phase is RuntimeSourceRestorePhase.DONE

    @property
    def failed(self) -> bool:
        """빠른 source 복원이 실패 상태인지 반환합니다."""

        return self._phase is RuntimeSourceRestorePhase.FAILED

    @property
    def progress(self) -> RuntimeSourceRestoreProgress:
        """현재 빠른 source 복원 진행값을 반환합니다."""

        return self._progress

    @property
    def result(self) -> LoadedReconstruction:
        """완료된 LoadedReconstruction 결과를 반환합니다."""

        if not self.done or self._result is None:
            raise RuntimeError("완료되지 않은 Runtime source 복원 결과를 읽을 수 없습니다.")
        return self._result

    @property
    def error(self) -> Exception | None:
        """실패한 빠른 source 복원의 원인을 반환합니다."""

        return self._error

    @property
    def resolved_layout(self) -> ResolvedLayout:
        """저장 경로에서 확정한 현재 COLMAP layout을 반환합니다."""

        return self._layout

    def advance(self, work_budget_ms: float | None = None) -> RuntimeSourceRestoreProgress:
        """한 Tick의 시간 예산 안에서 빠른 source 복원을 전진시킵니다."""

        self._validate_budget(work_budget_ms)
        if self.done:
            raise RuntimeError("완료된 Runtime source 복원 Job을 다시 실행할 수 없습니다.")
        if self.failed:
            raise RuntimeError("실패한 Runtime source 복원 Job을 다시 실행할 수 없습니다.")
        try:
            self._advance_current_phase(work_budget_ms)
        except Exception as exc:
            self._close_incremental_resources()
            self._error = exc
            self._phase = RuntimeSourceRestorePhase.FAILED
            self._progress = RuntimeSourceRestoreProgress(
                self._phase,
                self._progress.overall_fraction,
                self._progress.completed_units,
                self._progress.total_units,
                _PHASE_MESSAGES[self._phase],
            )
            raise
        return self._progress

    def abort(self) -> None:
        """열린 해시·Parser 자원을 닫고 부분 복원 결과를 폐기합니다."""

        if self.done or self.failed:
            return
        self._close_incremental_resources()
        self._error = RuntimeError("Runtime source 복원이 취소되었습니다.")
        self._phase = RuntimeSourceRestorePhase.FAILED
        self._progress = RuntimeSourceRestoreProgress(
            self._phase,
            self._progress.overall_fraction,
            self._progress.completed_units,
            self._progress.total_units,
            "Runtime source 복원 취소",
        )

    @staticmethod
    def _validate_budget(work_budget_ms: float | None) -> None:
        """증분 실행 budget이 양의 유한값인지 확인합니다."""

        if work_budget_ms is None:
            return
        if (
            isinstance(work_budget_ms, bool)
            or not isinstance(work_budget_ms, (int, float))
            or not math.isfinite(work_budget_ms)
            or work_budget_ms <= 0.0
        ):
            raise ValueError("Runtime source 복원 budget은 양의 유한값이어야 합니다.")

    def _snapshot(
        self,
        phase: RuntimeSourceRestorePhase,
        *,
        phase_fraction: float,
        completed_units: int,
        total_units: int,
    ) -> RuntimeSourceRestoreProgress:
        """단계 내부 진행률을 전체 단조 증가 진행률로 변환합니다."""

        start, end = _PHASE_RANGES[phase]
        overall = start + ((end - start) * phase_fraction)
        if hasattr(self, "_progress"):
            overall = max(overall, self._progress.overall_fraction)
        return RuntimeSourceRestoreProgress(
            phase,
            min(1.0, overall),
            int(completed_units),
            int(total_units),
            _PHASE_MESSAGES[phase],
        )

    def _enter_phase(
        self,
        phase: RuntimeSourceRestorePhase,
        *,
        phase_fraction: float = 0.0,
        completed_units: int = 0,
        total_units: int = 0,
    ) -> None:
        """현재 단계를 바꾸고 대응하는 진행 snapshot을 갱신합니다."""

        self._phase = phase
        self._progress = self._snapshot(
            phase,
            phase_fraction=phase_fraction,
            completed_units=completed_units,
            total_units=total_units,
        )

    def _advance_current_phase(self, work_budget_ms: float | None) -> None:
        """현재 단계에 대응하는 증분 작업 하나를 실행합니다."""

        if self._phase is RuntimeSourceRestorePhase.CREATED:
            self._enter_phase(RuntimeSourceRestorePhase.VERIFY_SOURCE)
            return
        if self._phase is RuntimeSourceRestorePhase.VERIFY_SOURCE:
            self._verify_source_step(work_budget_ms)
            return
        if self._phase is RuntimeSourceRestorePhase.READ_MODEL:
            self._read_model_step(work_budget_ms)
            return
        if self._phase is RuntimeSourceRestorePhase.VALIDATE_IMAGES:
            self._validate_images_step(work_budget_ms)
            return
        if self._phase is RuntimeSourceRestorePhase.BUILD_RESULT:
            self._build_result()
            self._enter_phase(
                RuntimeSourceRestorePhase.DONE,
                phase_fraction=1.0,
                completed_units=1,
                total_units=1,
            )
            return
        raise RuntimeError(f"실행할 수 없는 Runtime source 단계입니다: {self._phase.name}")

    def _verify_source_step(self, work_budget_ms: float | None) -> None:
        """현재 source 전체 SHA-256을 정확히 한 번 계산해 저장값과 비교합니다."""

        if self._fingerprint_job is None:
            job = source_fingerprint(self._layout, incremental=True)
            if not isinstance(job, SourceFingerprintJob):
                raise RuntimeError("증분 source fingerprint Job을 만들 수 없습니다.")
            self._fingerprint_job = job
        self._fingerprint_job.advance(work_budget_ms)
        if self._fingerprint_job.done:
            current = self._fingerprint_job.result
            self._signatures = self._fingerprint_job.signature_snapshot
            self._fingerprint_job.close()
            self._fingerprint_job = None
            if current != self._plan.expected_source_fingerprint:
                raise ValueError(SOURCE_CHANGED_REIMPORT_MESSAGE)
            self._enter_phase(RuntimeSourceRestorePhase.READ_MODEL)
            return
        total = self._fingerprint_job.total_bytes
        completed = self._fingerprint_job.completed_bytes
        self._enter_phase(
            RuntimeSourceRestorePhase.VERIFY_SOURCE,
            phase_fraction=completed / total if total else 1.0,
            completed_units=completed,
            total_units=total,
        )

    def _read_model_step(self, work_budget_ms: float | None) -> None:
        """검증된 source와 같은 형식으로 Reconstruction만 증분 해석합니다."""

        if self._read_job is None:
            self._read_job = ReconstructionReadJob(
                self._layout.model_directory,
                input_format=self._layout.input_format,
                modern=self._layout.layout == "modern",
            )
        self._read_job.advance(work_budget_ms)
        if self._read_job.done:
            self._model = self._read_job.result
            self._read_job.close()
            self._read_job = None
            try:
                verify_source_signatures(self._layout, self._signatures)
            except ValueError as exc:
                raise ValueError(
                    "COLMAP 데이터를 읽는 동안 원본 파일이 변경되었습니다. "
                    "기존 편집 기록은 적용하지 않았습니다. "
                    "COLMAP 데이터를 다시 Import하십시오."
                ) from exc
            self._image_records = tuple(self._model.images.values())
            self._enter_phase(
                RuntimeSourceRestorePhase.VALIDATE_IMAGES,
                total_units=len(self._image_records),
            )
            return
        total = self._read_job.total_units
        completed = self._read_job.completed_units
        self._enter_phase(
            RuntimeSourceRestorePhase.READ_MODEL,
            phase_fraction=completed / total if total else 1.0,
            completed_units=completed,
            total_units=total,
        )

    def _validate_images_step(self, work_budget_ms: float | None) -> None:
        """Camera Review용 이미지 경로를 다시 확인하고 절대경로를 고정합니다."""

        if self._image_job is None:
            image_job = validate_image_files(
                self._plan.images_directory,
                [image.name for image in self._image_records],
                incremental=True,
            )
            if not isinstance(image_job, ImagePathValidationJob):
                raise RuntimeError("증분 이미지 경로 검사 Job을 만들 수 없습니다.")
            self._image_job = image_job
        self._image_job.advance(work_budget_ms)
        if self._image_job.done:
            self._resolved_image_paths = self._image_job.result
            self._image_job = None
            self._enter_phase(RuntimeSourceRestorePhase.BUILD_RESULT)
            return
        total = self._image_job.total_units
        completed = self._image_job.completed_units
        self._enter_phase(
            RuntimeSourceRestorePhase.VALIDATE_IMAGES,
            phase_fraction=completed / total if total else 1.0,
            completed_units=completed,
            total_units=total,
        )

    def _build_result(self) -> None:
        """신뢰 승계 token과 Image Manifest를 포함한 Loader 동등 결과를 구성합니다."""

        if self._model is None:
            raise RuntimeError("Runtime source 복원 결과를 구성할 Reconstruction이 없습니다.")
        model = freeze_validated_reconstruction(self._model)
        descriptor = SourceDescriptor(
            dataset_root=Path(self._plan.dataset_root),
            sparse_root=sparse_root_for(Path(self._plan.model_directory)),
            model_directory=Path(self._plan.model_directory),
            images_directory=Path(self._plan.images_directory),
            input_format=self._layout.input_format,
            layout=self._layout.layout,
            selected_files=self._layout.selected_files,
            source_fingerprint=self._plan.expected_source_fingerprint,
            dataset_uuid=self._plan.expected_dataset_uuid,
            schema_version=self._plan.expected_schema_version,
        )
        validation_token = ValidatedReconstructionToken(
            reconstruction=model,
            dataset_uuid=self._plan.expected_dataset_uuid,
            source_fingerprint=self._plan.expected_source_fingerprint,
            validation_revision=0,
            cameras=model.cameras,
            images=model.images,
            points3d=model.points3d,
            rigs=model.rigs,
            frames=model.frames,
        )
        image_path_manifest = ImagePathManifest(
            dataset_uuid=self._plan.expected_dataset_uuid,
            source_fingerprint=self._plan.expected_source_fingerprint,
            images_directory=Path(self._plan.images_directory),
            entries=MappingProxyType(
                {
                    image.image_id: ValidatedImagePath(
                        image_id=image.image_id,
                        image_name=image.name,
                        resolved_image_path=resolved_path,
                        path_status="EXACT",
                    )
                    for image, resolved_path in zip(
                        self._image_records,
                        self._resolved_image_paths,
                        strict=True,
                    )
                }
            ),
        )
        self._result = LoadedReconstruction(
            model,
            descriptor,
            (),
            validation_token=validation_token,
            image_path_manifest=image_path_manifest,
        )

    def _close_incremental_resources(self) -> None:
        """열린 fingerprint와 Reconstruction stream을 안전하게 닫습니다."""

        if self._fingerprint_job is not None:
            self._fingerprint_job.close()
        if self._read_job is not None:
            self._read_job.close()


__all__ = [
    "SOURCE_CHANGED_REIMPORT_MESSAGE",
    "RuntimeSourceRestoreJob",
    "RuntimeSourceRestorePhase",
    "RuntimeSourceRestorePlan",
    "RuntimeSourceRestoreProgress",
]
