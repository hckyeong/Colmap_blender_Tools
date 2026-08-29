"""A/P 역할과 확정·드래프트 마커 번호를 2D 오버레이로 표시합니다."""

from __future__ import annotations

from collections.abc import Mapping

import blf
import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

from ..constants import (
    DRAFT_MODE_EDIT,
    ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    ICP_ALIGNMENT_PHASE_PREPARING,
    ICP_ALIGNMENT_PHASE_PREVIEW,
    ICP_ALIGNMENT_PHASE_RUNNING,
    ROLE_MOVING,
    ROLE_REFERENCE,
    marker_pair_color,
)
from ..logging_utils import get_logger
from ..state import RUNTIME
from .picking import marker_source_model_ids
from .point_data import effective_position_for_model_id


_LOGGER = get_logger("viewport.labels")
_DISPLAY_TRANSFORM_ERROR_MODEL_IDS: set[str] = set()
_ICP_STAGE_LABELS = {
    "PREPARE_INPUT": "입력 준비",
    "FIND_OVERLAP": "겹침 후보 탐색",
    "BUILD_COARSE": "낮은 해상도 준비",
    "ICP_COARSE": "낮은 해상도 ICP",
    "BUILD_MEDIUM": "중간 해상도 준비",
    "ICP_MEDIUM": "중간 해상도 ICP",
    "BUILD_FINE": "높은 해상도 준비",
    "ICP_FINE": "높은 해상도 ICP",
    "EVALUATE": "품질 평가",
    "WRITE_RESULT": "결과 기록",
    "COMPLETED": "완료",
}


def draw_text(
    text: str,
    x: float,
    y: float,
    size: float,
    color: tuple[float, float, float, float],
) -> None:
    """Blender 기본 글꼴로 UTF-8 오버레이 문구 한 줄을 그립니다."""
    font_id = 0
    blf.size(font_id, size)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0.0)
    blf.draw(font_id, text)


def _project(region, region_3d, position: tuple[float, float, float]):
    """3차원 마커 좌표를 현재 Region 픽셀 좌표로 투영합니다."""
    return view3d_utils.location_3d_to_region_2d(
        region,
        region_3d,
        Vector(position),
        default=None,
    )


def _marker_labels(role: str):
    """현재 보이는 모델의 확정·드래프트 라벨만 중복 없이 반환합니다."""
    state = RUNTIME.marker_selection
    labels = []
    visible_model_ids = set(marker_source_model_ids(role))
    draft = state.draft
    suppressed_pair_id = draft.source_pair_id if draft is not None else None
    prefix = "A" if role == ROLE_REFERENCE else "P"
    for pair in state.completed_pairs:
        if pair.pair_id == suppressed_pair_id:
            continue
        point = pair.reference if role == ROLE_REFERENCE else pair.moving
        if point.model_id and point.model_id not in visible_model_ids:
            continue
        labels.append(
            (
                f"{prefix}{pair.pair_id}",
                point.local_position,
                pair.pair_id,
                pair.pair_id == state.active_pair_id,
                point.model_id,
            )
        )
    if draft is not None:
        point = draft.reference if role == ROLE_REFERENCE else draft.moving
        if point is not None:
            if point.model_id and point.model_id not in visible_model_ids:
                return labels
            # 생성·편집 드래프트는 이전 UIList 선택과 무관하게 현재 후보를 강조합니다.
            labels.append(
                (
                    f"{prefix}{draft.pair_id}",
                    point.local_position,
                    draft.pair_id,
                    True,
                    point.model_id,
                )
            )
    return labels


def _marker_label_color(pair_id: int) -> tuple[float, float, float, float]:
    """마커 번호 라벨에 사용할 쌍 공통 색상을 반환합니다."""
    return marker_pair_color(pair_id)


def _display_label_position(model_id: str, source_position):
    """원본 마커 좌표를 점·원 마커와 같은 현재 Project World 좌표로 변환합니다."""
    return effective_position_for_model_id(model_id, source_position)


def _log_display_transform_error_once(model_id: str, error: Exception) -> None:
    """라벨 변환 실패를 프레임마다 반복하지 않고 한 번만 구조화 로그로 남깁니다."""
    normalized_model_id = str(model_id)
    if normalized_model_id in _DISPLAY_TRANSFORM_ERROR_MODEL_IDS:
        return
    _DISPLAY_TRANSFORM_ERROR_MODEL_IDS.add(normalized_model_id)
    _LOGGER.error(
        "event=label_display_transform_unavailable model_id=%s error_type=%s reason=%s",
        normalized_model_id,
        type(error).__name__,
        error,
    )


def clear_display_transform_errors() -> None:
    """새 세션에서 같은 모델 ID의 축 변환 오류를 다시 진단할 수 있게 초기화합니다."""
    _DISPLAY_TRANSFORM_ERROR_MODEL_IDS.clear()


def _draft_guide(role: str) -> str:
    """신규·통합 편집의 후보 상태와 명시 확정 단계를 화면 안내 문구로 만듭니다."""
    state = RUNTIME.marker_selection
    draft = state.draft
    if draft is None:
        return ""
    reference_status = "완료" if draft.reference is not None else "대기"
    moving_status = "완료" if draft.moving is not None else "대기"
    if state.can_confirm_draft():
        return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · N 패널에서 쌍 지정"
    if draft.mode == DRAFT_MODE_EDIT:
        return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · 이 뷰에서 점을 변경"
    return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · 이 뷰에서 점을 선택"


def _marker_preview_summary(role: str) -> str:
    """P 뷰에만 표시할 현재 marker preview 수치 요약을 만듭니다."""
    if role != ROLE_MOVING:
        return ""
    preview_state = getattr(RUNTIME, "marker_alignment", None)
    result = getattr(preview_state, "result", None)
    if preview_state is None or not preview_state.is_active or result is None:
        return ""
    metrics = result.metrics
    return (
        f"Marker Align · {metrics.count} pairs · "
        f"Scale {metrics.uniform_scale:.6g} · RMS {metrics.rms:.4g}"
    )


def _icp_preview_summary(role: str) -> str:
    """P 뷰에만 현재 ICP 단계 또는 결과 품질을 한 줄로 표시합니다."""
    if role != ROLE_MOVING:
        return ""
    state = getattr(RUNTIME, "icp_alignment", None)
    phase = str(getattr(state, "phase", ""))
    if phase in {
        ICP_ALIGNMENT_PHASE_PREPARING,
        ICP_ALIGNMENT_PHASE_RUNNING,
        ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    }:
        progress = getattr(state, "progress", None)
        stage = _ICP_STAGE_LABELS.get(
            str(getattr(progress, "stage", "")),
            "계산 중",
        )
        try:
            percentage = int(round(float(getattr(progress, "progress", 0.0)) * 100.0))
        except (TypeError, ValueError):
            percentage = 0
        suffix = " · 취소 대기" if phase == ICP_ALIGNMENT_PHASE_CANCEL_PENDING else ""
        return f"ICP · {stage} · {max(0, min(100, percentage))}%{suffix}"
    if phase != ICP_ALIGNMENT_PHASE_PREVIEW:
        return ""
    result = getattr(state, "result", None)
    quality = getattr(result, "quality", {})
    if not isinstance(quality, Mapping):
        quality = {}
    status = {"PASS": "통과", "REVIEW": "검토 필요", "REJECT": "거부"}.get(
        str(quality.get("status", "")).upper(),
        "결과 확인",
    )
    try:
        fitness = f" · Fitness {float(quality['fitness']):.4g}"
    except (KeyError, TypeError, ValueError):
        fitness = ""
    return f"ICP 결과 · {status}{fitness}"


def draw_viewport_labels(role: str) -> None:
    """현재 레이어, Marker Align, 드래프트 안내와 마커 번호를 화면에 그립니다."""
    context = bpy.context
    region = context.region
    space = context.space_data
    if region is None or space is None or not hasattr(space, "region_3d"):
        return
    region_3d = space.region_3d
    role_text = "활성 레이어" if role == ROLE_REFERENCE else "선택 레이어"
    draw_text(role_text, 18.0, region.height - 30.0, 16.0, (1.0, 1.0, 1.0, 0.95))

    preview_summary = _marker_preview_summary(role)
    if preview_summary:
        draw_text(
            preview_summary,
            18.0,
            region.height - 54.0,
            14.0,
            (1.0, 0.78, 0.22, 0.95),
        )

    icp_summary = _icp_preview_summary(role)
    if icp_summary:
        draw_text(
            icp_summary,
            18.0,
            region.height - 76.0,
            14.0,
            (0.48, 0.88, 1.0, 0.95),
        )

    guide = _draft_guide(role)
    if guide:
        draw_text(guide, 18.0, 24.0, 14.0, (1.0, 0.82, 0.22, 1.0))

    for label, source_position, pair_id, is_active, model_id in _marker_labels(role):
        try:
            position = _display_label_position(model_id, source_position)
        except Exception as error:
            _log_display_transform_error_once(model_id, error)
            continue
        projected = _project(region, region_3d, position)
        if projected is None:
            continue
        color = _marker_label_color(pair_id)
        draw_text(label, projected.x + 9.0, projected.y + 7.0, 17.0 if is_active else 15.0, color)
