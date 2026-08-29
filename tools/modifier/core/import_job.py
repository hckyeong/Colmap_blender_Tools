"""COLMAP Import의 순수 Python 구간을 tick 사이에서 재진입 가능하게 실행합니다."""

from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from types import MappingProxyType

from .discovery import DiscoveryResult, _directories_to_depth_two
from .fingerprint import SourceFingerprintJob, source_fingerprint
from .image_path_job import ImagePathValidationJob
from .import_types import ImportPhase, ImportPlan, ImportProgressSnapshot
from .integrity_job import ReconstructionValidationJob
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
from .paths import validate_image_files
from .reconstruction_read_job import ReconstructionReadJob


SCHEMA_VERSION = 1

_PHASE_RANGES: dict[ImportPhase, tuple[float, float]] = {
    ImportPhase.CREATED: (0.0, 0.0),
    ImportPhase.DISCOVER: (0.0, 0.02),
    ImportPhase.HASH_SOURCE_BEFORE: (0.02, 0.14),
    ImportPhase.READ_MODEL: (0.14, 0.60),
    ImportPhase.VALIDATE_MODEL: (0.60, 0.82),
    ImportPhase.VALIDATE_IMAGES: (0.82, 0.90),
    ImportPhase.HASH_SOURCE_AFTER: (0.90, 0.98),
    ImportPhase.BUILD_RESULT: (0.98, 1.0),
    ImportPhase.DONE: (1.0, 1.0),
    ImportPhase.FAILED: (0.0, 1.0),
}

_PHASE_MESSAGES: dict[ImportPhase, str] = {
    ImportPhase.CREATED: "Import 작업 생성 완료",
    ImportPhase.DISCOVER: "Dataset 경로와 COLMAP 형식 탐색 중",
    ImportPhase.HASH_SOURCE_BEFORE: "원본 COLMAP 파일 SHA-256 계산 중",
    ImportPhase.READ_MODEL: "COLMAP Reconstruction 해석 중",
    ImportPhase.VALIDATE_MODEL: "Reconstruction 무결성 검사 중",
    ImportPhase.VALIDATE_IMAGES: "Image 경로와 파일 검사 중",
    ImportPhase.HASH_SOURCE_AFTER: "원본 COLMAP 파일 변경 여부 확인 중",
    ImportPhase.BUILD_RESULT: "검증된 Import 결과 구성 중",
    ImportPhase.DONE: "COLMAP Import Core 완료",
    ImportPhase.FAILED: "COLMAP Import Core 실패",
}


def freeze_validated_reconstruction(model: Reconstruction) -> Reconstruction:
    """검증된 mapping을 읽기 전용으로 바꾸고 token 수명 중 교체를 감지합니다."""

    model.cameras = MappingProxyType(model.cameras)
    model.images = MappingProxyType(model.images)
    model.points3d = MappingProxyType(model.points3d)
    model.rigs = MappingProxyType(model.rigs)
    model.frames = MappingProxyType(model.frames)
    return model


class ImportLoadJob:
    """기존 Loader의 결과와 오류 의미를 보존하는 증분 Import Core Job입니다."""

    def __init__(self, plan: ImportPlan) -> None:
        if not isinstance(plan, ImportPlan):
            raise ValueError("유효한 ImportPlan이 필요합니다.")
        self._plan = plan
        self._phase = ImportPhase.CREATED
        self._started_at = time.monotonic()
        self._progress = self._snapshot(
            ImportPhase.CREATED,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
        )
        self._result: LoadedReconstruction | None = None
        self._error: Exception | None = None
        self._discovery_result: DiscoveryResult | None = None
        self._layout: ResolvedLayout | None = None
        self._model_directory: Path | None = None
        self._images_directory: Path | None = None
        self._before_fingerprint: str | None = None
        self._after_fingerprint: str | None = None
        self._model: Reconstruction | None = None
        self._warnings: tuple[str, ...] = ()
        self._image_records = ()
        self._resolved_image_paths: tuple[Path, ...] = ()
        self._image_candidates: tuple[Path, ...] = ()
        self._valid_image_candidates: list[tuple[Path, tuple[Path, ...]]] = []
        self._image_candidate_errors: list[ValueError] = []
        self._image_candidate_index = 0
        self._dataset_uuid: str | None = None
        self._fingerprint_job: SourceFingerprintJob | None = None
        self._read_job: ReconstructionReadJob | None = None
        self._validation_job: ReconstructionValidationJob | None = None
        self._image_job: ImagePathValidationJob | None = None

    @property
    def plan(self) -> ImportPlan:
        return self._plan

    @property
    def done(self) -> bool:
        return self._phase is ImportPhase.DONE

    @property
    def failed(self) -> bool:
        return self._phase is ImportPhase.FAILED

    @property
    def progress(self) -> ImportProgressSnapshot:
        return self._progress

    @property
    def result(self) -> LoadedReconstruction:
        if not self.done or self._result is None:
            raise RuntimeError("완료되지 않은 Import Job의 결과를 읽을 수 없습니다.")
        return self._result

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def discovery_result(self) -> DiscoveryResult | None:
        return self._discovery_result

    @property
    def resolved_layout(self) -> ResolvedLayout | None:
        return self._layout

    @property
    def resolved_sparse_directory(self) -> Path | None:
        return self._model_directory

    @property
    def resolved_images_directory(self) -> Path | None:
        return self._images_directory

    @property
    def resolved_dataset_uuid(self) -> str | None:
        return self._dataset_uuid

    def advance(
        self,
        work_budget_ms: float | None = None,
    ) -> ImportProgressSnapshot:
        """다음 안전 경계까지 실행하고 현재 진행 상태를 반환합니다."""

        self._validate_budget(work_budget_ms)
        if self.done:
            raise RuntimeError("완료된 Import Job을 다시 실행할 수 없습니다.")
        if self.failed:
            raise RuntimeError("실패한 Import Job을 다시 실행할 수 없습니다.")

        try:
            self._advance_current_phase(work_budget_ms)
        except Exception as exc:
            self._close_incremental_resources()
            self._error = exc
            previous_fraction = self._progress.overall_fraction
            self._phase = ImportPhase.FAILED
            self._progress = ImportProgressSnapshot(
                phase=ImportPhase.FAILED,
                overall_fraction=previous_fraction,
                phase_fraction=0.0,
                completed_units=0,
                total_units=0,
                message=_PHASE_MESSAGES[ImportPhase.FAILED],
                elapsed_seconds=self._elapsed_seconds(),
                can_cancel=False,
            )
            raise
        return self._progress

    def abort(self) -> None:
        """파일 load·Undo·Extension 해제 경계에서 열린 stream을 정리합니다."""

        if self.done or self.failed:
            return
        self._close_incremental_resources()
        self._error = RuntimeError("완료되지 않은 Import Job이 강제 종료되었습니다.")
        previous_fraction = self._progress.overall_fraction
        self._phase = ImportPhase.FAILED
        self._progress = ImportProgressSnapshot(
            phase=ImportPhase.FAILED,
            overall_fraction=previous_fraction,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
            message=_PHASE_MESSAGES[ImportPhase.FAILED],
            elapsed_seconds=self._elapsed_seconds(),
            can_cancel=False,
        )

    def _close_incremental_resources(self) -> None:
        if self._fingerprint_job is not None:
            self._fingerprint_job.close()
        if self._read_job is not None:
            self._read_job.close()

    @staticmethod
    def _validate_budget(work_budget_ms: float | None) -> None:
        if work_budget_ms is None:
            return
        if (
            isinstance(work_budget_ms, bool)
            or not isinstance(work_budget_ms, (int, float))
            or not math.isfinite(work_budget_ms)
            or work_budget_ms <= 0.0
        ):
            raise ValueError("Import 작업 budget은 0보다 큰 유한한 수치여야 합니다.")

    def _elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_at)

    def _snapshot(
        self,
        phase: ImportPhase,
        *,
        phase_fraction: float,
        completed_units: int,
        total_units: int,
    ) -> ImportProgressSnapshot:
        start, end = _PHASE_RANGES[phase]
        overall = start + ((end - start) * phase_fraction)
        if hasattr(self, "_progress"):
            overall = max(overall, self._progress.overall_fraction)
        return ImportProgressSnapshot(
            phase=phase,
            overall_fraction=overall,
            phase_fraction=phase_fraction,
            completed_units=completed_units,
            total_units=total_units,
            message=_PHASE_MESSAGES[phase],
            elapsed_seconds=self._elapsed_seconds(),
            can_cancel=False,
        )

    def _enter_phase(
        self,
        phase: ImportPhase,
        *,
        completed_units: int = 0,
        total_units: int = 0,
        phase_fraction: float = 0.0,
    ) -> None:
        self._phase = phase
        self._progress = self._snapshot(
            phase,
            phase_fraction=phase_fraction,
            completed_units=completed_units,
            total_units=total_units,
        )

    def _advance_current_phase(self, work_budget_ms: float | None) -> None:
        if self._phase is ImportPhase.CREATED:
            self._enter_phase(ImportPhase.DISCOVER)
            return
        if self._phase is ImportPhase.DISCOVER:
            self._discover()
            self._enter_phase(ImportPhase.HASH_SOURCE_BEFORE)
            return
        if self._phase is ImportPhase.HASH_SOURCE_BEFORE:
            self._hash_source_step(work_budget_ms, after_read=False)
            return
        if self._phase is ImportPhase.READ_MODEL:
            self._read_model_step(work_budget_ms)
            return
        if self._phase is ImportPhase.VALIDATE_MODEL:
            self._validate_model_step(work_budget_ms)
            return
        if self._phase is ImportPhase.VALIDATE_IMAGES:
            self._validate_images_step(work_budget_ms)
            return
        if self._phase is ImportPhase.HASH_SOURCE_AFTER:
            self._hash_source_step(work_budget_ms, after_read=True)
            return
        if self._phase is ImportPhase.BUILD_RESULT:
            self._build_result()
            self._enter_phase(
                ImportPhase.DONE,
                completed_units=1,
                total_units=1,
                phase_fraction=1.0,
            )
            return
        raise RuntimeError(f"실행할 수 없는 Import 단계입니다: {self._phase.name}")

    def _discover(self) -> None:
        root = Path(self._plan.dataset_root)
        if not root.is_dir():
            raise ValueError(f"Dataset Root가 존재하지 않습니다: {root}")

        sparse = self._plan.sparse_directory
        images = self._plan.images_directory
        directories = _directories_to_depth_two(root)
        sparse_candidates: list[Path] = []
        if sparse is None:
            for directory in directories:
                try:
                    resolve_layout(directory, self._plan.input_format)
                except ValueError:
                    continue
                sparse_candidates.append(directory)
            sparse_candidates = sorted(set(sparse_candidates), key=str)
            standard_sparse_zero = (root / "sparse" / "0").resolve()
            standard_sparse = (root / "sparse").resolve()
            if standard_sparse_zero in sparse_candidates:
                sparse = standard_sparse_zero
            elif standard_sparse in sparse_candidates:
                sparse = standard_sparse
            elif len(sparse_candidates) == 1:
                sparse = sparse_candidates[0]
        else:
            sparse_candidates = [Path(sparse).expanduser().resolve()]

        if sparse is None:
            if not sparse_candidates:
                raise ValueError("Sparse 후보를 찾지 못했습니다.")
            raise ValueError(
                f"Sparse 후보가 {len(sparse_candidates)}개입니다. 직접 선택하십시오."
            )

        self._model_directory = Path(sparse).expanduser().resolve()
        self._layout = resolve_layout(
            self._model_directory,
            self._plan.input_format,
        )
        if images is None:
            self._image_candidates = tuple(
                sorted(
                    {
                        directory
                        for directory in directories
                        if directory.name.lower() == "images"
                    },
                    key=str,
                )
            )
        else:
            self._image_candidates = (
                Path(images).expanduser().resolve(),
            )
        if not self._image_candidates:
            raise ValueError("검증된 Images 후보를 찾지 못했습니다.")
        if len(self._image_candidates) == 1:
            self._images_directory = self._image_candidates[0]

        self._discovery_result = DiscoveryResult(
            sparse_directory=self._model_directory,
            images_directory=self._images_directory,
            status="Dataset 후보 경로를 찾았으며 Image 파일 검증을 대기 중입니다.",
            sparse_candidates=tuple(sparse_candidates),
            images_candidates=self._image_candidates,
        )
        if (
            self._plan.matching_dataset_uuid is not None
            and (
                self._plan.matching_model_directory is None
                or self._plan.matching_model_directory == self._model_directory
            )
        ):
            self._dataset_uuid = self._plan.matching_dataset_uuid
        else:
            self._dataset_uuid = str(uuid.uuid4())

    def _hash_source_step(
        self,
        work_budget_ms: float | None,
        *,
        after_read: bool,
    ) -> None:
        if self._layout is None:
            raise RuntimeError("지문을 계산할 COLMAP layout이 없습니다.")
        if self._fingerprint_job is None:
            fingerprint_job = source_fingerprint(
                self._layout,
                incremental=True,
            )
            if not isinstance(fingerprint_job, SourceFingerprintJob):
                raise RuntimeError("증분 source fingerprint Job을 만들 수 없습니다.")
            self._fingerprint_job = fingerprint_job

        self._fingerprint_job.advance(work_budget_ms)
        if self._fingerprint_job.done:
            fingerprint = self._fingerprint_job.result
            self._fingerprint_job.close()
            self._fingerprint_job = None
            if after_read:
                self._after_fingerprint = fingerprint
                if self._before_fingerprint != self._after_fingerprint:
                    raise ValueError(
                        "Import 중 COLMAP source 파일이 변경되었습니다. 다시 시도하십시오."
                    )
                self._enter_phase(ImportPhase.BUILD_RESULT)
            else:
                self._before_fingerprint = fingerprint
                self._enter_phase(ImportPhase.READ_MODEL)
            return

        total = self._fingerprint_job.total_bytes
        completed = self._fingerprint_job.completed_bytes
        self._enter_phase(
            (
                ImportPhase.HASH_SOURCE_AFTER
                if after_read
                else ImportPhase.HASH_SOURCE_BEFORE
            ),
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _read_model_step(self, work_budget_ms: float | None) -> None:
        if self._layout is None or self._model_directory is None:
            raise RuntimeError("해석할 COLMAP layout이 없습니다.")
        if self._read_job is None:
            self._read_job = ReconstructionReadJob(
                self._model_directory,
                input_format=self._layout.input_format,
                modern=self._layout.layout == "modern",
            )
        self._read_job.advance(work_budget_ms)
        if self._read_job.done:
            self._model = self._read_job.result
            self._read_job.close()
            self._read_job = None
            self._enter_phase(ImportPhase.VALIDATE_MODEL)
            return

        total = self._read_job.total_units
        completed = self._read_job.completed_units
        self._enter_phase(
            ImportPhase.READ_MODEL,
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _validate_model_step(self, work_budget_ms: float | None) -> None:
        if self._model is None:
            raise RuntimeError("검사할 Reconstruction이 없습니다.")
        if self._validation_job is None:
            self._validation_job = ReconstructionValidationJob(self._model)
        self._validation_job.advance(work_budget_ms)
        if self._validation_job.done:
            self._warnings = self._validation_job.result
            self._image_records = tuple(self._model.images.values())
            self._validation_job = None
            self._enter_phase(
                ImportPhase.VALIDATE_IMAGES,
                total_units=len(self._image_records),
            )
            return

        self._enter_phase(
            ImportPhase.VALIDATE_MODEL,
            completed_units=self._validation_job.completed_units,
            total_units=self._validation_job.total_units,
            phase_fraction=self._validation_job.fraction,
        )

    def _validate_images_step(self, work_budget_ms: float | None) -> None:
        if not self._image_candidates:
            raise RuntimeError("검사할 Images Directory 후보가 없습니다.")
        image_count = len(self._image_records)
        candidate_count = len(self._image_candidates)
        if self._image_candidate_index >= candidate_count:
            self._finish_image_candidate_validation()
            return
        candidate = self._image_candidates[self._image_candidate_index]
        if self._image_job is None:
            try:
                image_job = validate_image_files(
                    candidate,
                    [image.name for image in self._image_records],
                    incremental=True,
                )
            except ValueError as exc:
                self._reject_current_image_candidate(
                    exc,
                    image_count=image_count,
                    candidate_count=candidate_count,
                )
                return
            if not isinstance(image_job, ImagePathValidationJob):
                raise RuntimeError("증분 이미지 경로 검사 Job을 만들 수 없습니다.")
            self._image_job = image_job
        try:
            self._image_job.advance(work_budget_ms)
        except ValueError as exc:
            self._reject_current_image_candidate(
                exc,
                image_count=image_count,
                candidate_count=candidate_count,
            )
            return
        if self._image_job.done:
            self._valid_image_candidates.append(
                (candidate, self._image_job.result)
            )
            self._image_job = None
            self._image_candidate_index += 1
            if self._image_candidate_index == candidate_count:
                self._finish_image_candidate_validation()
                return
            self._enter_phase(
                ImportPhase.VALIDATE_IMAGES,
                completed_units=self._image_candidate_index * image_count,
                total_units=candidate_count * image_count,
                phase_fraction=self._image_candidate_index / candidate_count,
            )
            return

        total = candidate_count * image_count
        completed = (
            self._image_candidate_index * image_count
            + self._image_job.completed_units
        )
        self._enter_phase(
            ImportPhase.VALIDATE_IMAGES,
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _reject_current_image_candidate(
        self,
        error: ValueError,
        *,
        image_count: int,
        candidate_count: int,
    ) -> None:
        self._image_candidate_errors.append(error)
        self._image_job = None
        self._image_candidate_index += 1
        if self._image_candidate_index == candidate_count:
            self._finish_image_candidate_validation()
            return
        self._enter_phase(
            ImportPhase.VALIDATE_IMAGES,
            completed_units=self._image_candidate_index * image_count,
            total_units=candidate_count * image_count,
            phase_fraction=self._image_candidate_index / candidate_count,
        )

    def _finish_image_candidate_validation(self) -> None:
        if not self._valid_image_candidates:
            if len(self._image_candidates) == 1 and self._image_candidate_errors:
                raise self._image_candidate_errors[0]
            raise ValueError("검증된 Images 후보를 찾지 못했습니다.")

        standard_images = (Path(self._plan.dataset_root) / "images").resolve()
        selected = next(
            (
                item
                for item in self._valid_image_candidates
                if item[0] == standard_images
            ),
            None,
        )
        if selected is None:
            if len(self._valid_image_candidates) != 1:
                raise ValueError(
                    f"Images 후보가 {len(self._valid_image_candidates)}개입니다. "
                    "직접 선택하십시오."
                )
            selected = self._valid_image_candidates[0]

        self._images_directory, self._resolved_image_paths = selected
        self._discovery_result = DiscoveryResult(
            sparse_directory=self._model_directory,
            images_directory=self._images_directory,
            status="Sparse Directory와 검증된 Images Directory를 찾았습니다.",
            sparse_candidates=(
                self._discovery_result.sparse_candidates
                if self._discovery_result is not None
                else ()
            ),
            images_candidates=tuple(
                path for path, _resolved in self._valid_image_candidates
            ),
        )
        self._enter_phase(ImportPhase.HASH_SOURCE_AFTER)

    def _build_result(self) -> None:
        if (
            self._layout is None
            or self._model_directory is None
            or self._images_directory is None
            or self._model is None
            or self._after_fingerprint is None
            or self._dataset_uuid is None
        ):
            raise RuntimeError("검증된 Import 결과를 구성할 수 없습니다.")

        model = freeze_validated_reconstruction(self._model)
        descriptor = SourceDescriptor(
            dataset_root=Path(self._plan.dataset_root),
            sparse_root=sparse_root_for(self._model_directory),
            model_directory=self._model_directory,
            images_directory=self._images_directory,
            input_format=self._layout.input_format,
            layout=self._layout.layout,
            selected_files=self._layout.selected_files,
            source_fingerprint=self._after_fingerprint,
            dataset_uuid=self._dataset_uuid,
            schema_version=SCHEMA_VERSION,
        )
        validation_token = ValidatedReconstructionToken(
            reconstruction=model,
            dataset_uuid=self._dataset_uuid,
            source_fingerprint=self._after_fingerprint,
            validation_revision=0,
            cameras=model.cameras,
            images=model.images,
            points3d=model.points3d,
            rigs=model.rigs,
            frames=model.frames,
        )
        image_path_manifest = ImagePathManifest(
            dataset_uuid=self._dataset_uuid,
            source_fingerprint=self._after_fingerprint,
            images_directory=self._images_directory,
            entries={
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
            },
        )
        self._result = LoadedReconstruction(
            model,
            descriptor,
            self._warnings,
            validation_token=validation_token,
            image_path_manifest=image_path_manifest,
        )


__all__ = [
    "ImportLoadJob",
    "SCHEMA_VERSION",
    "freeze_validated_reconstruction",
]
