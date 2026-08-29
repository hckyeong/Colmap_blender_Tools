"""Blender UI 설정을 검증된 COLMAP Core Import 요청으로 변환합니다."""

from __future__ import annotations

from pathlib import Path
import uuid

import bpy

from ..core.colmap.camera_import_job import (
    ImportStagingPreparationJob,
    ImportStagingRevalidationJob,
    revalidate_import_staging,
)
from ..core.colmap.discovery import discover_dataset
from ..core.colmap.errors import ColmapError
from .camera_import import CameraImportRequest


class CameraImportRequestJob:
    """Scene 입력 Snapshot에서 Camera Import 요청을 증분 구성합니다."""

    def __init__(self, context) -> None:
        """Blender RNA 값을 Main Thread에서 복사하고 Core Job을 생성합니다."""

        settings = context.scene.projection_paint
        sparse, images = _input_directories(settings)
        self._dataset_uuid = uuid.uuid4().hex
        self._dataset_revision = int(settings.dataset_revision) + 1
        self._camera_axis_preset = str(settings.camera_axis_preset)
        self._camera_forward = str(settings.camera_forward)
        self._camera_up = str(settings.camera_up)
        self._point_size = float(settings.point_size)
        self._camera_frustum_size = float(settings.camera_frustum_size)
        self._job = ImportStagingPreparationJob(
            sparse,
            images,
            input_format=str(settings.input_format),
            frame_start=int(settings.frame_start),
            axis_preset=self._camera_axis_preset,
            custom_forward=self._camera_forward,
            custom_up=self._camera_up,
        )
        self._result = None

    @property
    def done(self) -> bool:
        """Camera Import 요청이 완성되었는지 반환합니다."""

        return self._result is not None

    @property
    def progress(self):
        """Core 준비 Job의 현재 진행 Snapshot을 반환합니다."""

        return self._job.progress

    @property
    def result(self) -> CameraImportRequest:
        """완료된 Camera Import 요청을 반환합니다."""

        if self._result is None:
            raise RuntimeError("완료되지 않은 Camera Import 요청 Job입니다.")
        return self._result

    def advance(self, work_budget_ms: float | None = 25.0):
        """시간 예산 안에서 Core 준비를 진행하고 완료 시 요청을 고정합니다."""

        if self.done:
            raise RuntimeError("완료된 Camera Import 요청 Job을 다시 실행할 수 없습니다.")
        progress = self._job.advance(work_budget_ms=work_budget_ms)
        if self._job.done:
            staging = self._job.result

            def source_validator() -> bool:
                """Commit 직전 COLMAP과 이미지 파일 Identity를 다시 검증합니다."""

                revalidate_import_staging(staging)
                return True

            self._result = CameraImportRequest(
                dataset_uuid=self._dataset_uuid,
                dataset_revision=self._dataset_revision,
                source_fingerprint=staging.source_fingerprint,
                cameras=staging.dataset.cameras,
                images=tuple(staging.dataset.images.values()),
                points=staging.points,
                frame_start=staging.frame_mapping.frame_start,
                axis_preset=self._camera_axis_preset,
                custom_forward=self._camera_forward,
                custom_up=self._camera_up,
                source_validator=source_validator,
                source_validation_job_factory=(
                    lambda: ImportStagingRevalidationJob(staging)
                ),
                model_root=str(staging.dataset.layout.model_directory),
                images_root=str(staging.image_manifest.images_root),
                image_paths={
                    image_id: str(entry.resolved_path)
                    for image_id, entry in staging.image_manifest.entries.items()
                },
                point_size=self._point_size,
                camera_frustum_size=self._camera_frustum_size,
                core_validated=True,
            )
        return progress

    def abort(self) -> None:
        """열린 Core Parser를 닫고 요청 준비를 중단합니다."""

        self._job.abort()


def _absolute_path(value: str) -> Path:
    """Blender 상대경로를 확장한 절대 Path로 변환합니다."""

    return Path(bpy.path.abspath(str(value))).expanduser()


def _input_directories(settings) -> tuple[Path, Path]:
    """Dataset Root를 우선하고 없으면 명시한 두 입력 경로를 사용합니다."""

    root_text = str(settings.dataset_root).strip()
    if root_text:
        discovered = discover_dataset(
            _absolute_path(root_text),
            str(settings.input_format),
        )
        if (
            discovered.sparse_directory is None
            or discovered.images_directory is None
        ):
            raise ColmapError(discovered.status)
        return discovered.sparse_directory, discovered.images_directory
    sparse = (
        _absolute_path(settings.sparse_directory)
        if str(settings.sparse_directory).strip()
        else None
    )
    images = (
        _absolute_path(settings.images_directory)
        if str(settings.images_directory).strip()
        else None
    )
    if sparse is not None and images is not None:
        return sparse, images
    raise ColmapError(
        "Dataset Root 또는 Sparse·Images Directory를 모두 지정하십시오."
    )


def build_camera_import_request(context) -> CameraImportRequest:
    """Scene 설정에서 완전히 검증된 Camera Import 요청을 만듭니다."""
    job = CameraImportRequestJob(context)
    while not job.done:
        job.advance(work_budget_ms=None)
    return job.result


def create_camera_import_request_job(context) -> CameraImportRequestJob:
    """GUI Modal Import용 증분 Camera Import 요청 Job을 생성합니다."""

    return CameraImportRequestJob(context)


__all__ = [
    "CameraImportRequestJob",
    "build_camera_import_request",
    "create_camera_import_request_job",
]
