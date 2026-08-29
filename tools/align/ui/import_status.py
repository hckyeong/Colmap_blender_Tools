"""COLMAP 가져오기 작업을 N 패널 표시용 불변 값으로 변환합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from ..display_axes import axis_summary
from ..importer import (
    IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE,
    IMPORT_STAGE_READ_MODEL,
    import_stage_label,
)
from ..mesh_importer import MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE, mesh_import_stage_label


_READER_STAGE_LABELS = {
    "RIGS": "리그 데이터 읽기",
    "CAMERAS": "카메라 데이터 읽기",
    "FRAMES": "프레임 데이터 읽기",
    "IMAGES": "이미지와 2D 관측점 읽기",
    "POINTS3D": "3D 점과 관측 트랙 읽기",
}


@dataclass(frozen=True, slots=True)
class ImportStatusView:
    """N 패널 한 번의 draw에서 사용할 가져오기 진행 정보를 보관합니다."""

    fraction: float
    percentage_text: str
    stage_text: str
    detail_stage_text: str
    processed_text: str
    elapsed_text: str
    dataset_text: str
    source_text: str
    format_text: str
    axis_text: str


def compact_path(path: str | Path, maximum: int = 44) -> str:
    """좁은 N 패널에서 마지막 폴더를 보존하도록 긴 경로 앞부분을 줄입니다."""
    if maximum < 2:
        raise ValueError("maximum은 2 이상이어야 합니다.")
    text = str(path)
    if len(text) <= maximum:
        return text
    return f"…{text[-(maximum - 1):]}"


def _format_elapsed(seconds: float) -> str:
    """경과 초를 N 패널에서 빠르게 읽을 수 있는 한국어 시간으로 표시합니다."""
    elapsed = max(0.0, float(seconds))
    if elapsed < 60.0:
        return f"{elapsed:.1f}초"
    minutes = int(elapsed // 60.0)
    return f"{minutes}분 {elapsed - minutes * 60.0:04.1f}초"


def _format_processed(job: Any) -> str:
    """현재 단계의 처리량과 단위를 작업 상태에 맞춰 표시합니다."""
    current_count = max(0, int(getattr(job, "current_count", 0)))
    total_count = max(0, int(getattr(job, "total_count", 0)))
    stage = str(getattr(job, "stage", ""))
    if stage in {IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE, MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE}:
        unit = "점"
    else:
        unit = "바이트" if "HASH_SOURCE" in stage else "레코드"
    if total_count <= 0:
        if current_count > 0:
            return f"{current_count:,} {unit} 처리됨 (전체 수 확인 중)"
        return "작업량 계산 중"
    return f"{min(current_count, total_count):,} / {total_count:,} {unit}"


def _format_input(job: Any) -> str:
    """탐색 후 결정된 입력 형식과 COLMAP 레이아웃을 한 줄로 표시합니다."""
    selected_format = str(getattr(job, "selected_format", "")).strip()
    if not selected_format and job.__class__.__name__ == "MeshImportJob":
        return "OBJ / Triangle Mesh"
    layout = getattr(job, "layout", None)
    layout_kind = str(getattr(layout, "layout_kind", "")).strip()
    if selected_format and layout_kind:
        return f"{selected_format.title()} / {layout_kind.title()}"
    return str(getattr(job, "input_format", "AUTO")).title()


def _detail_stage_text(job: Any) -> str:
    """읽기 또는 표시 샘플링 단계의 현재 세부 정보를 표시합니다."""
    stage = str(getattr(job, "stage", ""))
    if stage == IMPORT_STAGE_READ_MODEL:
        reader_stage = str(getattr(job, "reader_stage", ""))
        return _READER_STAGE_LABELS.get(reader_stage, "읽기 준비")
    if stage in {IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE, MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE}:
        display_point_limit = getattr(job, "display_point_limit", getattr(job, "display_vertex_limit", None))
        if display_point_limit is not None:
            return f"표시 상한: 모델당 {int(display_point_limit):,}"
    return ""


def import_status_view(job: Any, *, now: float | None = None) -> ImportStatusView:
    """실행 중 ImportJob을 GPU와 무관한 N 패널 표시 값으로 변환합니다."""
    fraction = max(0.0, min(float(getattr(job, "progress", 0.0)), 1.0))
    current_time = perf_counter() if now is None else float(now)
    started_at = float(getattr(job, "started_at", current_time))
    dataset_root = str(getattr(job, "dataset_root", "") or getattr(job, "source_path", ""))
    source_path = str(getattr(job, "source_path", ""))
    transform = getattr(job, "display_transform", None)
    return ImportStatusView(
        fraction=fraction,
        percentage_text=f"{int(round(fraction * 100.0))}%",
        stage_text=(
            mesh_import_stage_label(str(getattr(job, "stage", "")))
            if job.__class__.__name__ == "MeshImportJob"
            else import_stage_label(str(getattr(job, "stage", "")))
        ),
        detail_stage_text=_detail_stage_text(job),
        processed_text=_format_processed(job),
        elapsed_text=_format_elapsed(current_time - started_at),
        dataset_text=compact_path(dataset_root),
        source_text=(
            compact_path(source_path)
            if source_path and Path(source_path) != Path(dataset_root)
            else ""
        ),
        format_text=_format_input(job),
        axis_text=axis_summary(transform) if transform is not None else "확인 중",
    )


__all__ = ("ImportStatusView", "compact_path", "import_status_view")
