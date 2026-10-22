"""검증된 Camera/Image를 Blender 연결 전 Staging Payload로 조립합니다."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
from time import perf_counter

from ..axis import axes_for_preset, axis_matrix
from .camera_models import pinhole_parameters
from .errors import ColmapError
from .fingerprint import semantic_fingerprint_steps
from .frame_mapping import build_frame_mapping
from .image_paths import (
    build_image_path_manifest_steps,
)
from .io_common import CancelCheck, check_cancel
from .layout import resolve_layout
from .model import (
    DEFAULT_LIMITS,
    CameraDataPayload,
    CameraObjectPayload,
    ColmapDataset,
    ImportStagingPayload,
    MarkerPayload,
    ParserLimits,
    PointPayload,
    ResolvedLayout,
)
from .shared_reader import PointPayloadView, PointRecordMapping, shared_dataset_steps
from .validation import (
    normalized_quaternion,
    validate_camera_image_records,
    validate_camera_image_records_steps,
    validate_point3d_records,
    validate_point3d_records_steps,
)


class ImportPreparationPhase(str, Enum):
    """Core Import 준비 작업의 사용자 표시 단계를 정의합니다."""

    CREATED = "CREATED"
    RESOLVE_LAYOUT = "RESOLVE_LAYOUT"
    READ_CAMERAS = "READ_CAMERAS"
    READ_IMAGES = "READ_IMAGES"
    READ_POINTS = "READ_POINTS"
    VALIDATE_CAMERAS = "VALIDATE_CAMERAS"
    VALIDATE_POINTS = "VALIDATE_POINTS"
    VALIDATE_IMAGE_PATHS = "VALIDATE_IMAGE_PATHS"
    FINGERPRINT = "FINGERPRINT"
    BUILD_RESULT = "BUILD_RESULT"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ImportPreparationProgress:
    """Core 준비 단계의 전체 비율과 현재 실제 처리량을 보관합니다."""

    phase: ImportPreparationPhase
    overall_fraction: float
    completed_units: int
    total_units: int
    message: str


@dataclass(frozen=True, slots=True)
class ImportSourceSnapshot:
    """재검증 비교에 필요한 Dataset, Image Manifest와 Fingerprint를 묶습니다."""

    dataset: ColmapDataset
    image_manifest: object
    source_fingerprint: str


_PHASE_RANGES = {
    ImportPreparationPhase.CREATED: (0.0, 0.0),
    ImportPreparationPhase.RESOLVE_LAYOUT: (0.0, 0.02),
    ImportPreparationPhase.READ_CAMERAS: (0.02, 0.07),
    ImportPreparationPhase.READ_IMAGES: (0.07, 0.22),
    ImportPreparationPhase.READ_POINTS: (0.22, 0.55),
    ImportPreparationPhase.VALIDATE_CAMERAS: (0.55, 0.62),
    ImportPreparationPhase.VALIDATE_POINTS: (0.62, 0.70),
    ImportPreparationPhase.VALIDATE_IMAGE_PATHS: (0.70, 0.80),
    ImportPreparationPhase.FINGERPRINT: (0.80, 0.90),
    ImportPreparationPhase.BUILD_RESULT: (0.90, 1.0),
    ImportPreparationPhase.DONE: (1.0, 1.0),
    ImportPreparationPhase.FAILED: (0.0, 1.0),
}

_PHASE_MESSAGES = {
    ImportPreparationPhase.CREATED: "Preparing COLMAP import",
    ImportPreparationPhase.RESOLVE_LAYOUT: "Resolving COLMAP layout",
    ImportPreparationPhase.READ_CAMERAS: "Reading camera records",
    ImportPreparationPhase.READ_IMAGES: "Reading image pose records",
    ImportPreparationPhase.READ_POINTS: "Reading Point3D records",
    ImportPreparationPhase.VALIDATE_CAMERAS: "Validating cameras and images",
    ImportPreparationPhase.VALIDATE_POINTS: "Validating Point3D records",
    ImportPreparationPhase.VALIDATE_IMAGE_PATHS: "Validating source image paths",
    ImportPreparationPhase.FINGERPRINT: "Calculating source fingerprint",
    ImportPreparationPhase.BUILD_RESULT: "Building Blender import payload",
    ImportPreparationPhase.DONE: "COLMAP data preparation complete",
    ImportPreparationPhase.FAILED: "COLMAP data preparation failed",
}


def load_camera_image_dataset(
    layout: ResolvedLayout,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> ColmapDataset:
    """확정 Layout의 Reconstruction을 읽고 의미 무결성을 검증합니다."""
    from .io_common import consume_generator

    cameras, images, points3d, identities, file_fingerprint = consume_generator(
        shared_dataset_steps(layout, limits, cancel_check)
    )
    validate_camera_image_records(cameras, images, limits)
    validate_point3d_records(points3d, limits)
    check_cancel(cancel_check)
    return ColmapDataset(
        layout=layout,
        cameras=cameras,
        images=images,
        points3d=points3d,
        source_identities=identities,
        source_file_fingerprint=file_fingerprint,
    )


def _build_import_payload_steps(
    dataset: ColmapDataset,
    image_manifest,
    source_fingerprint: str,
    *,
    frame_start: int,
    axis_preset: str,
    custom_forward: str | None,
    custom_up: str | None,
) -> Generator[tuple[int, int], None, ImportStagingPayload]:
    """Blender 전달 Payload를 레코드 단위로 구성해 처리량을 전달합니다."""

    frame_mapping = build_frame_mapping(
        dataset.images.values(),
        frame_start=frame_start,
    )
    forward, up = axes_for_preset(
        axis_preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
    )
    camera_data = []
    camera_objects = []
    markers = []
    points = []
    total = len(dataset.cameras) + len(dataset.images) + len(dataset.points3d)
    completed = 0
    yield completed, total
    for camera_id in sorted(dataset.cameras):
        camera = dataset.cameras[camera_id]
        fx, fy, cx, cy = pinhole_parameters(camera.model_name, camera.params)
        camera_data.append(
            CameraDataPayload(
                camera_id=camera.camera_id,
                model_name=camera.model_name,
                width=camera.width,
                height=camera.height,
                fx=fx,
                fy=fy,
                cx=cx,
                cy=cy,
            )
        )
        completed += 1
        if completed % 256 == 0:
            yield completed, total
    for image_id in sorted(dataset.images):
        image = dataset.images[image_id]
        camera_object = CameraObjectPayload(
            image_id=image.image_id,
            camera_id=image.camera_id,
            image_name=image.image_name,
            qvec=normalized_quaternion(image.qvec),
            tvec=image.tvec,
            frame=frame_mapping.image_to_frame[image_id],
        )
        camera_objects.append(camera_object)
        markers.append(
            MarkerPayload(frame=camera_object.frame, image_id=image.image_id)
        )
        completed += 1
        if completed % 256 == 0:
            yield completed, total
    for point3d_id in (() if isinstance(dataset.points3d, PointRecordMapping) else sorted(dataset.points3d)):
        point = dataset.points3d[point3d_id]
        points.append(
            PointPayload(
                point3d_id=point.point3d_id,
                xyz=point.xyz,
                rgb=point.rgb,
                error=point.error,
            )
        )
        completed += 1
        if completed % 256 == 0:
            yield completed, total
    return ImportStagingPayload(
        dataset=dataset,
        image_manifest=image_manifest,
        frame_mapping=frame_mapping,
        source_fingerprint=source_fingerprint,
        axis_matrix=axis_matrix(forward, up),
        camera_data=tuple(camera_data),
        camera_objects=tuple(camera_objects),
        points=(PointPayloadView(dataset.points3d) if isinstance(dataset.points3d, PointRecordMapping) else tuple(points)),
        markers=tuple(markers),
        warnings=(),
    )


class ImportStagingPreparationJob:
    """COLMAP 읽기·검증·Payload 구성을 시간 예산 안에서 증분 실행합니다."""

    def __init__(
        self,
        model_directory: Path | str,
        images_directory: Path | str,
        *,
        input_format: str = "AUTO",
        frame_start: int = 1,
        axis_preset: str = "REALITY_CAPTURE",
        custom_forward: str | None = None,
        custom_up: str | None = None,
        allow_extension_fallback: bool = False,
        limits: ParserLimits = DEFAULT_LIMITS,
        cancel_check: CancelCheck = None,
        build_payload: bool = True,
        point_limit: int | None = None,
    ) -> None:
        """불변 입력과 선택적인 참고 점 상한을 복사해 증분 준비 상태를 만듭니다."""

        self._model_directory = model_directory
        self._images_directory = images_directory
        self._input_format = input_format
        self._frame_start = int(frame_start)
        self._axis_preset = axis_preset
        self._custom_forward = custom_forward
        self._custom_up = custom_up
        self._allow_extension_fallback = bool(allow_extension_fallback)
        self._limits = limits
        self._cancel_check = cancel_check
        self._build_payload = bool(build_payload)
        self._point_limit = point_limit
        self._phase = ImportPreparationPhase.CREATED
        self._progress = self._snapshot(self._phase, 0, 0)
        self._iterator = None
        self._layout = None
        self._cameras = None
        self._images = None
        self._points = None
        self._identities = []
        self._dataset = None
        self._image_manifest = None
        self._fingerprint = None
        self._result = None
        self._source_snapshot = None
        self._error = None

    @property
    def done(self) -> bool:
        """Core 준비 Payload가 완성되었는지 반환합니다."""

        return self._phase is ImportPreparationPhase.DONE

    @property
    def progress(self) -> ImportPreparationProgress:
        """현재 Core 준비 단계의 불변 진행 Snapshot을 반환합니다."""

        return self._progress

    @property
    def result(self) -> ImportStagingPayload:
        """완료된 Core 준비 Payload를 반환합니다."""

        if not self.done or self._result is None:
            raise RuntimeError("완료되지 않은 COLMAP 준비 Job의 결과입니다.")
        return self._result

    @property
    def source_snapshot(self) -> ImportSourceSnapshot:
        """Payload 생성을 생략한 완료 Job의 Source Snapshot을 반환합니다."""

        if not self.done or self._source_snapshot is None:
            raise RuntimeError("완료되지 않은 Source Snapshot Job입니다.")
        return self._source_snapshot

    def _complete(self) -> None:
        """현재 준비 Job을 100% 완료 상태로 전환합니다."""

        self._phase = ImportPreparationPhase.DONE
        self._iterator = None
        self._progress = ImportPreparationProgress(
            phase=self._phase,
            overall_fraction=1.0,
            completed_units=1,
            total_units=1,
            message=_PHASE_MESSAGES[self._phase],
        )

    def _snapshot(
        self,
        phase: ImportPreparationPhase,
        completed: int,
        total: int,
    ) -> ImportPreparationProgress:
        """단계 처리량을 단조 증가하는 전체 진행률로 변환합니다."""

        fraction = completed / total if total else 0.0
        start, end = _PHASE_RANGES[phase]
        overall = start + (end - start) * fraction
        if hasattr(self, "_progress"):
            overall = max(overall, self._progress.overall_fraction)
        return ImportPreparationProgress(
            phase=phase,
            overall_fraction=min(1.0, overall),
            completed_units=max(0, int(completed)),
            total_units=max(0, int(total)),
            message=_PHASE_MESSAGES[phase],
        )

    def _enter(
        self,
        phase: ImportPreparationPhase,
        iterator=None,
    ) -> None:
        """다음 준비 단계와 선택적 증분 Generator를 활성화합니다."""

        self._phase = phase
        self._iterator = iterator
        self._progress = self._snapshot(phase, 0, 0)

    def _parser_steps(self, kind: str):
        """공개 전체 읽기는 유지하고 요청된 참고 상한만 공통 파서에 전달합니다."""
        del kind
        self._shared_reading = True
        return shared_dataset_steps(self._layout, self._limits, self._cancel_check, point_limit=self._point_limit)

    def _finish_phase(self, value) -> None:
        """완료된 생성기 결과를 저장하고 다음 준비 단계로 전환합니다."""
        if getattr(self, "_shared_reading", False):
            self._shared_reading = False
            self._cameras, self._images, self._points, identities, file_fingerprint = value
            self._dataset = ColmapDataset(
                layout=self._layout,
                cameras=self._cameras,
                images=self._images,
                points3d=self._points,
                source_identities=identities,
                source_file_fingerprint=file_fingerprint,
            )
            self._enter(
                ImportPreparationPhase.VALIDATE_CAMERAS,
                validate_camera_image_records_steps(self._dataset.cameras, self._dataset.images, self._limits),
            )
            return
        if self._phase is ImportPreparationPhase.VALIDATE_CAMERAS:
            self._enter(
                ImportPreparationPhase.VALIDATE_POINTS,
                validate_point3d_records_steps(
                    self._dataset.points3d,
                    self._limits,
                ),
            )
            return
        if self._phase is ImportPreparationPhase.VALIDATE_POINTS:
            self._enter(
                ImportPreparationPhase.VALIDATE_IMAGE_PATHS,
                build_image_path_manifest_steps(
                    self._images_directory,
                    self._dataset.images.values(),
                    allow_extension_fallback=self._allow_extension_fallback,
                    limits=self._limits,
                    cancel_check=self._cancel_check,
                ),
            )
            return
        if self._phase is ImportPreparationPhase.VALIDATE_IMAGE_PATHS:
            self._image_manifest = value
            self._enter(
                ImportPreparationPhase.FINGERPRINT,
                semantic_fingerprint_steps(self._dataset),
            )
            return
        if self._phase is ImportPreparationPhase.FINGERPRINT:
            self._fingerprint = value
            if not self._build_payload:
                self._source_snapshot = ImportSourceSnapshot(
                    dataset=self._dataset,
                    image_manifest=self._image_manifest,
                    source_fingerprint=self._fingerprint,
                )
                self._complete()
                return
            self._enter(
                ImportPreparationPhase.BUILD_RESULT,
                _build_import_payload_steps(
                    self._dataset,
                    self._image_manifest,
                    self._fingerprint,
                    frame_start=self._frame_start,
                    axis_preset=self._axis_preset,
                    custom_forward=self._custom_forward,
                    custom_up=self._custom_up,
                ),
            )
            return
        if self._phase is ImportPreparationPhase.BUILD_RESULT:
            self._result = value
            self._complete()
            return
        raise RuntimeError(f"완료할 수 없는 준비 단계입니다: {self._phase}")

    def advance(self, work_budget_ms: float | None = 25.0) -> ImportPreparationProgress:
        """지정된 시간 예산 안에서 현재 Core 세부 작업을 진행합니다."""

        if self.done:
            raise RuntimeError("완료된 COLMAP 준비 Job을 다시 실행할 수 없습니다.")
        if work_budget_ms is not None and (
            isinstance(work_budget_ms, bool)
            or not isinstance(work_budget_ms, (int, float))
            or not isfinite(work_budget_ms)
            or work_budget_ms <= 0.0
        ):
            raise ValueError("Import 준비 시간 예산은 0보다 큰 유한값이어야 합니다.")
        deadline = (
            None
            if work_budget_ms is None
            else perf_counter() + float(work_budget_ms) / 1000.0
        )
        try:
            if self._phase is ImportPreparationPhase.CREATED:
                self._enter(ImportPreparationPhase.RESOLVE_LAYOUT)
                return self._progress
            if self._phase is ImportPreparationPhase.RESOLVE_LAYOUT:
                self._layout = resolve_layout(
                    self._model_directory,
                    self._input_format,
                )
                self._progress = self._snapshot(self._phase, 1, 1)
                self._enter(
                    ImportPreparationPhase.READ_CAMERAS,
                    self._parser_steps("cameras"),
                )
                return self._progress
            while self._iterator is not None:
                try:
                    update = next(self._iterator)
                    completed, total = update[:2]
                    if len(update) == 3:
                        self._phase = {
                            "CAMERAS": ImportPreparationPhase.READ_CAMERAS,
                            "IMAGES": ImportPreparationPhase.READ_IMAGES,
                            "POINTS3D": ImportPreparationPhase.READ_POINTS,
                            "COMPLETED": ImportPreparationPhase.READ_POINTS,
                        }.get(update[2], self._phase)
                except StopIteration as finished:
                    self._finish_phase(finished.value)
                    return self._progress
                self._progress = self._snapshot(self._phase, completed, total)
                if deadline is not None and perf_counter() >= deadline:
                    return self._progress
            raise RuntimeError(f"준비 Generator가 없습니다: {self._phase}")
        except Exception as exc:
            self._error = exc
            self._phase = ImportPreparationPhase.FAILED
            self._progress = ImportPreparationProgress(
                phase=self._phase,
                overall_fraction=self._progress.overall_fraction,
                completed_units=0,
                total_units=0,
                message=_PHASE_MESSAGES[self._phase],
            )
            self.abort()
            raise

    def run_to_completion(self) -> ImportStagingPayload:
        """Background와 테스트에서 준비 Job을 동기 완료합니다."""

        while not self.done:
            self.advance(work_budget_ms=None)
        return self.result

    def release_source_data(self) -> None:
        """재검증 완료 뒤 중복 Source Dataset과 Manifest 참조를 해제합니다."""

        self._source_snapshot = None
        self._dataset = None
        self._image_manifest = None
        self._cameras = None
        self._images = None
        self._points = None
        self._identities.clear()
        self._shared_result = None

    def abort(self) -> None:
        """열린 증분 Parser를 닫고 보유 중인 Generator를 해제합니다."""

        iterator = self._iterator
        self._iterator = None
        if iterator is not None:
            iterator.close()
        if not self.done:
            self.release_source_data()


def prepare_import_staging(
    model_directory: Path | str,
    images_directory: Path | str,
    *,
    input_format: str = "AUTO",
    frame_start: int = 1,
    axis_preset: str = "REALITY_CAPTURE",
    custom_forward: str | None = None,
    custom_up: str | None = None,
    allow_extension_fallback: bool = False,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> ImportStagingPayload:
    """COLMAP 입력을 Scene 외부의 완전한 Import 준비 Payload로 만듭니다."""
    return ImportStagingPreparationJob(
        model_directory,
        images_directory,
        input_format=input_format,
        frame_start=frame_start,
        axis_preset=axis_preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
        allow_extension_fallback=allow_extension_fallback,
        limits=limits,
        cancel_check=cancel_check,
    ).run_to_completion()


class ImportStagingRevalidationJob:
    """확정 전 파일 내용·경로를 검사하고 최초 불변 파싱 결과를 재사용합니다."""

    def __init__(
        self,
        expected: ImportStagingPayload,
        *,
        limits: ParserLimits = DEFAULT_LIMITS,
        cancel_check: CancelCheck = None,
    ) -> None:
        """재파싱 없이 원본 해시와 이미지 경로만 검사하는 상태를 준비합니다."""
        self._expected = expected
        self._limits = limits
        self._cancel_check = cancel_check
        self._fingerprint_job = None
        self._image_steps = None
        self._progress = ImportPreparationProgress(ImportPreparationPhase.CREATED, 0.0, 0, 0, "Preparing source checks")
        self._done = False

    @property
    def done(self) -> bool:
        """Source 재검증과 최초 Snapshot 비교가 완료되었는지 반환합니다."""

        return self._done

    @property
    def progress(self) -> ImportPreparationProgress:
        """해시와 이미지 경로 검사만의 단조 증가하는 진행 상태를 반환합니다."""
        return self._progress

    def assert_current_files(self) -> None:
        """모델 선택과 원본 파일 식별자를 확정 경계에서도 다시 대조합니다."""
        from .io_common import identity_from_stat
        try:
            from .....shared.colmap_io import discover_sparse_model
        except ImportError:
            from shared.colmap_io import discover_sparse_model
        expected = self._expected.dataset
        selected = discover_sparse_model(expected.layout.model_directory, input_format=expected.layout.input_format)
        if tuple(selected.selected_files) != tuple(item.path for item in expected.source_identities):
            raise ColmapError("확정 직전 COLMAP 모델 파일 구성이 변경되었습니다.")
        for identity in expected.source_identities:
            if not identity.same_file(identity_from_stat(identity.path, identity.path.stat())):
                raise ColmapError("확정 직전 COLMAP 원본 파일 식별자가 변경되었습니다.")

    def _compare_images(self, current) -> None:
        """촬영 이미지의 실제 경로·상태·파일 식별을 최초 결과와 비교합니다."""
        if set(current.entries) != set(self._expected.image_manifest.entries):
            raise ColmapError("Commit 직전 Image 경로 Manifest가 변경되었습니다.")
        for image_id, expected in self._expected.image_manifest.entries.items():
            actual = current.entries[image_id]
            if (
                actual.image_name != expected.image_name
                or actual.normalized_relative_path
                != expected.normalized_relative_path
                or actual.resolved_path != expected.resolved_path
                or actual.path_status != expected.path_status
                or not expected.file_identity.same_file(actual.file_identity)
            ):
                raise ColmapError(
                    f"Commit 직전 IMAGE_ID {image_id}의 Source가 변경되었습니다."
                )

    def advance(self, work_budget_ms: float | None = 25.0) -> ImportPreparationProgress:
        """시간 예산 안에서 파일 해시·이미지 경로 검사를 실행하고 원본 변경을 거부합니다."""
        if self._done:
            raise RuntimeError("완료된 Source 재검증 Job을 다시 실행할 수 없습니다.")
        if work_budget_ms is not None and (isinstance(work_budget_ms, bool) or not isinstance(work_budget_ms, (int, float)) or not isfinite(work_budget_ms) or work_budget_ms <= 0):
            raise ValueError("원본 검사 시간 예산은 0보다 큰 유한값이어야 합니다.")
        deadline = None if work_budget_ms is None else perf_counter() + work_budget_ms / 1000.0
        try:
            check_cancel(self._cancel_check)
            if self._fingerprint_job is None:
                try:
                    from .....shared.colmap_io import SourceFingerprintJob, discover_sparse_model
                except ImportError:
                    from shared.colmap_io import SourceFingerprintJob, discover_sparse_model
                self.assert_current_files()
                layout = self._expected.dataset.layout
                if resolve_layout(layout.model_directory, layout.input_format) != layout:
                    raise ColmapError("확정 직전 COLMAP 모델 선택이 변경되었습니다.")
                self._fingerprint_job = SourceFingerprintJob(discover_sparse_model(layout.model_directory, input_format=layout.input_format))
            if not self._fingerprint_job.done:
                self._fingerprint_job.step(None if deadline is None else max(0.0, deadline - perf_counter()))
                completed, total = self._fingerprint_job.completed_bytes, self._fingerprint_job.total_bytes
                self._progress = ImportPreparationProgress(ImportPreparationPhase.FINGERPRINT, 0.8 * completed / max(1, total), completed, total, "Checking source file contents")
                if not self._fingerprint_job.done:
                    return self._progress
                if self._fingerprint_job.result != self._expected.dataset.source_file_fingerprint:
                    raise ColmapError("확정 직전 COLMAP 원본 파일의 내용이 변경되었습니다.")
                self.assert_current_files()
            if self._image_steps is None:
                self._image_steps = build_image_path_manifest_steps(
                    self._expected.image_manifest.images_root, self._expected.dataset.images.values(),
                    allow_extension_fallback=any(item.path_status == "FALLBACK" for item in self._expected.image_manifest.entries.values()),
                    limits=self._limits, cancel_check=self._cancel_check,
                )
            while True:
                try:
                    completed, total = next(self._image_steps)
                    self._progress = ImportPreparationProgress(ImportPreparationPhase.VALIDATE_IMAGE_PATHS, 0.8 + 0.2 * completed / max(1, total), completed, total, "Checking source image paths")
                except StopIteration as finished:
                    self._compare_images(finished.value)
                    self.assert_current_files()
                    self._done = True
                    self._progress = ImportPreparationProgress(ImportPreparationPhase.DONE, 1.0, 1, 1, "Source checks complete")
                    self.abort()
                    return self._progress
                if deadline is not None and perf_counter() >= deadline:
                    return self._progress
        except Exception:
            self.abort()
            raise

    def run_to_completion(self) -> None:
        """Background와 동기 경로에서 Source 재검증을 끝까지 실행합니다."""

        while not self.done:
            self.advance(work_budget_ms=None)

    def abort(self) -> None:
        """재파싱 작업 없이 원본 검사 파일과 이미지 생성기를 닫습니다."""
        if self._fingerprint_job is not None:
            self._fingerprint_job.close()
        if self._image_steps is not None:
            self._image_steps.close()


def revalidate_import_staging(
    payload: ImportStagingPayload,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> None:
    """Commit 직전에 Source 의미와 모든 이미지 File Identity를 재검증합니다."""
    ImportStagingRevalidationJob(
        payload,
        limits=limits,
        cancel_check=cancel_check,
    ).run_to_completion()
