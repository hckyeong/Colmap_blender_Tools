"""기존 동기 COLMAP Loader API를 증분 Import Core Job에 연결합니다."""

from __future__ import annotations

from pathlib import Path

from .import_job import (
    SCHEMA_VERSION,
    ImportLoadJob,
    freeze_validated_reconstruction,
)
from .import_types import ImportPlan
from .layout import resolve_layout
from .model import LoadedReconstruction, Reconstruction


def _freeze_validated_reconstruction(
    model: Reconstruction,
) -> Reconstruction:
    """기존 내부 이름을 유지하는 호환 facade입니다."""

    return freeze_validated_reconstruction(model)


def load_reconstruction(
    dataset_root: Path | str,
    model_directory: Path | str,
    images_directory: Path | str,
    input_format: str = "AUTO",
    dataset_uuid: str | None = None,
) -> LoadedReconstruction:
    """동기 호출도 증분 Job을 완료까지 drain해 동일한 의미를 보장합니다."""

    root = Path(dataset_root).expanduser().resolve()
    model_path = Path(model_directory).expanduser().resolve()
    images_path = Path(images_directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Dataset Root가 존재하지 않습니다: {root}")
    # 기존 Loader와 동일하게 Sparse 경로·형식 오류를 parser 실행 전에 확정합니다.
    resolve_layout(model_path, input_format)

    job = ImportLoadJob(
        ImportPlan(
            dataset_root=root,
            sparse_directory=model_path,
            images_directory=images_path,
            input_format=input_format,
            matching_dataset_uuid=dataset_uuid,
            matching_model_directory=(
                model_path if dataset_uuid is not None else None
            ),
        )
    )
    try:
        while not job.done:
            job.advance()
        return job.result
    except Exception:
        job.abort()
        raise


__all__ = [
    "ImportLoadJob",
    "ImportPlan",
    "SCHEMA_VERSION",
    "_freeze_validated_reconstruction",
    "load_reconstruction",
]
