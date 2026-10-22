"""Blender UI 설정을 검증된 COLMAP Core Import 요청으로 변환합니다."""

from __future__ import annotations

from datetime import datetime, timezone
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
        """파일 탐색기의 점 상한을 포함한 입력 값을 복사하고 참고용 Core Job을 생성합니다."""

        settings = context.scene.projection_paint
        if str(getattr(settings, "paint_state", "IDLE")) != "IDLE":
            raise ColmapError("페인팅을 종료한 뒤 COLMAP 입력을 교체하십시오.")
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
            point_limit=int(getattr(settings, "import_point_limit", 20_000)),
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
        """검증된 참고 결과와 원본 전체 메타데이터를 가져오기 요청으로 고정합니다."""

        if self.done:
            raise RuntimeError("완료된 Camera Import 요청 Job을 다시 실행할 수 없습니다.")
        progress = self._job.advance(work_budget_ms=work_budget_ms)
        if self._job.done:
            staging = self._job.result
            preview = getattr(staging.dataset.points3d, "preview", None)

            def source_validator() -> bool:
                """Commit 직전 COLMAP과 이미지 파일 Identity를 다시 검증합니다."""

                revalidate_import_staging(staging)
                return True

            self._result = CameraImportRequest(
                dataset_uuid=self._dataset_uuid,
                dataset_revision=self._dataset_revision,
                source_fingerprint=staging.dataset.source_file_fingerprint,
                cameras=staging.dataset.cameras,
                images=staging.camera_objects,
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
                source_kind="FILE",
                snapshot_schema_version=1,
                content_fingerprint=staging.dataset.source_file_fingerprint,
                snapshot_created_at=datetime.now(timezone.utc).isoformat(),
                reference_metadata={} if preview is None else {
                    "schema": 1, "policy": preview.sample_policy, "point_limit": preview.point_limit,
                    "source_point_count": preview.source_point_count, "imported_point_count": len(preview.points.point_ids),
                    "source_track_count": preview.source_track_count, "source_observation_count": preview.source_observation_count,
                    "bounds_min": preview.bounds_min.tolist(), "bounds_max": preview.bounds_max.tolist(),
                    "sample_fingerprint": staging.source_fingerprint,
                },
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
