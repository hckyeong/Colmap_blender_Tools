"""외부 COLMAP 설치 없이 sparse model을 안전하게 적재하는 순수 I/O 계층입니다."""

from __future__ import annotations

from pathlib import Path

from ..logging_utils import get_logger
from .binary_reader import (
    read_cameras_binary,
    read_frames_binary,
    read_images_binary,
    read_points3d_binary,
    read_rigs_binary,
)
from .discovery import (
    SparseModelLayout,
    discover_sparse_model,
    discover_sparse_model_with_dataset_root,
)
from .errors import (
    ColmapDiscoveryError,
    ColmapIOError,
    ColmapParseError,
    ColmapValidationError,
)
from .fingerprint import Sha256FileJob, SourceFingerprintJob, source_fingerprint
from .incremental_reader import (
    DEFAULT_MAX_RECORDS_PER_STEP,
    IncrementalReadProgress,
    IncrementalSparseModelReader,
    STAGE_CAMERAS,
    STAGE_CANCELLED,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_FRAMES,
    STAGE_IMAGES,
    STAGE_POINTS3D,
    STAGE_RIGS,
    create_incremental_sparse_model_reader,
)
from .text_reader import (
    read_cameras_text,
    read_frames_text,
    read_images_text,
    read_points3d_text,
    read_rigs_text,
)
from .types import (
    CameraRecord,
    ColmapModelData,
    FrameDataRecord,
    FrameRecord,
    ImageRecord,
    ImageTable,
    INVALID_POINT3D_ID,
    ParsedColmapModel,
    PointTable,
    RigRecord,
    RigSensorRecord,
)
from .validation import calculate_bounds, validate_model


LOGGER = get_logger("colmap_io")


def read_sparse_model(
    layout: SparseModelLayout,
    *,
    model_id: str = "",
    display_name: str | None = None,
    geometry_revision: int = 0,
) -> ColmapModelData:
    """탐색 완료 파일을 동기적으로 파싱만 하고 검증 전 모델을 반환합니다."""
    if not isinstance(layout, SparseModelLayout):
        raise TypeError("read_sparse_model에는 SparseModelLayout이 필요합니다.")
    LOGGER.info(
        "event=model_read_started stage=parse source=%s format=%s",
        layout.source_path,
        layout.selected_format,
    )
    reader = create_incremental_sparse_model_reader(
        layout,
        model_id=model_id,
        display_name=display_name,
        geometry_revision=geometry_revision,
    )
    try:
        while not reader.is_completed:
            reader.step(max_records=10_000_000)
        model = reader.model
    except ColmapIOError:
        LOGGER.warning(
            "event=model_read_failed stage=parse source=%s format=%s",
            layout.source_path,
            layout.selected_format,
        )
        raise
    finally:
        reader.close()
    LOGGER.info(
        "event=model_read_completed stage=complete camera_count=%d image_count=%d point_count=%d",
        len(model.cameras),
        model.images.count,
        model.points.count,
    )
    return model


def load_sparse_model(
    path: str | Path,
    *,
    input_format: str = "AUTO",
    model_id: str = "",
    display_name: str | None = None,
    geometry_revision: int = 0,
) -> ColmapModelData:
    """경로 탐색·파싱 뒤 엄격 검증을 정확히 한 번 수행해 모델을 반환합니다."""
    layout = discover_sparse_model(path, input_format=input_format)
    model = read_sparse_model(
        layout,
        model_id=model_id,
        display_name=display_name,
        geometry_revision=geometry_revision,
    )
    validate_model(model)
    return model


__all__ = (
    "CameraRecord",
    "ColmapDiscoveryError",
    "ColmapIOError",
    "ColmapModelData",
    "ColmapParseError",
    "ColmapValidationError",
    "DEFAULT_MAX_RECORDS_PER_STEP",
    "FrameDataRecord",
    "FrameRecord",
    "INVALID_POINT3D_ID",
    "ImageRecord",
    "ImageTable",
    "IncrementalReadProgress",
    "IncrementalSparseModelReader",
    "ParsedColmapModel",
    "PointTable",
    "RigRecord",
    "RigSensorRecord",
    "SparseModelLayout",
    "Sha256FileJob",
    "SourceFingerprintJob",
    "STAGE_CAMERAS",
    "STAGE_CANCELLED",
    "STAGE_COMPLETED",
    "STAGE_FAILED",
    "STAGE_FRAMES",
    "STAGE_IMAGES",
    "STAGE_POINTS3D",
    "STAGE_RIGS",
    "calculate_bounds",
    "discover_sparse_model",
    "discover_sparse_model_with_dataset_root",
    "create_incremental_sparse_model_reader",
    "load_sparse_model",
    "read_cameras_binary",
    "read_cameras_text",
    "read_frames_binary",
    "read_frames_text",
    "read_images_binary",
    "read_images_text",
    "read_points3d_binary",
    "read_points3d_text",
    "read_rigs_binary",
    "read_rigs_text",
    "read_sparse_model",
    "source_fingerprint",
    "validate_model",
)
