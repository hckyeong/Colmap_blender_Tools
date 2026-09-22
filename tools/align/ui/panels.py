"""전용 작업공간 오른쪽 뷰 N 패널의 COLMAP Align UI를 그립니다."""

from __future__ import annotations

from collections.abc import Mapping
import math
import unicodedata

import bpy

from ..alignment.icp_geometry import geometry_mode_summary
from ..axis_settings import axis_edit_active_block_reason, axis_edit_block_reason
from ..constants import (
    CONTROL_PANEL_CATEGORY,
    ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    MARKER_COMMAND_CANCEL,
    MODEL_STATUS_REGISTERED,
    ROLE_MOVING,
)
from ..operators.marker_alignment import (
    align_start_block_reason,
    automatic_alignment_description,
    direct_align_start_block_reason,
    icp_phase,
    is_icp_input_locked,
    is_icp_result_approval_available,
    is_icp_result_preview_active,
    is_icp_running,
    is_marker_preview_active,
    marker_preview_button_label,
    marker_preview_start_block_reason,
)
from ..operators.export_model import (
    format_merge_export_error,
    is_merge_export_input_locked,
    is_merge_export_running,
    merge_export_phase,
    merge_export_start_block_reason,
    merge_export_status_entries,
)
from ..operators.export_obj import (
    is_obj_export_input_locked,
    is_obj_export_running,
    obj_export_phase,
    obj_export_start_block_reason,
    obj_export_status_entries,
)
from ..properties import (
    marker_align_log_list,
    marker_pair_list,
    sync_marker_align_log,
)
from ..project_persistence.runtime import (
    restore_input_block_reason,
    restore_status_issue,
)
from ..session import is_align_workspace_for_window, is_session_active_for_window
from ..state import RUNTIME
from ..viewport.point_data import surface_model_data_for_id
from ..viewport.registry import role_for_area
from .import_status import import_status_view


def _configure_layout(layout) -> None:
    """N 패널의 간결한 2열 속성 정렬과 장식 숨김을 적용합니다."""
    layout.use_property_split = True
    layout.use_property_decorate = False


def _moving_panel_poll(context) -> bool:
    """현재 컨텍스트가 READY Align Workspace의 오른쪽 데이터 레이어 뷰인지 확인합니다."""
    return bool(
        context.window
        and context.area
        and is_align_workspace_for_window(context.window)
        and context.area.type == "VIEW_3D"
        and role_for_area(context.window, context.area) == ROLE_MOVING
    )


def _set_session_layout_enabled(layout, context) -> None:
    """세션 재연결 전에도 UI는 유지하되 사용자 입력만 잠시 비활성화합니다."""

    layout.enabled = is_session_active_for_window(getattr(context, "window", None))

class COLMAPALIGN_UL_marker_pairs(bpy.types.UIList):
    """pair_id 기반 WindowManager 미러를 선택 가능한 목록으로 표시합니다."""

    bl_idname = "COLMAPALIGN_UL_marker_pairs"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index=0):
        """확정 여부와 안정 pair_id를 한 행으로 표시합니다."""
        layout.label(text=item.summary, icon="CHECKMARK")


class COLMAPALIGN_UL_marker_align_log(bpy.types.UIList):
    """Marker Align의 최신 상태 메시지 한 줄을 표시합니다."""

    bl_idname = "COLMAPALIGN_UL_marker_align_log"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index=0):
        """아이콘 없이 축소된 크기로 상태 로그 한 줄을 표시합니다."""
        layout.scale_y = 0.8
        layout.label(text=item.message)


def _text_display_units(text: str) -> int:
    """한글·전각 문자를 두 칸으로 계산해 패널 폭과 가까운 표시 길이를 반환합니다."""
    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F", "A"} else 1
        for character in str(text)
    )


def _split_log_token(token: str, maximum_units: int) -> tuple[str, ...]:
    """공백 없는 긴 토큰을 표시 단위 한도 안의 조각으로 나눕니다."""
    pieces: list[str] = []
    current = ""
    for character in str(token):
        candidate = current + character
        if current and _text_display_units(candidate) > maximum_units:
            pieces.append(current)
            current = character
        else:
            current = candidate
    if current:
        pieces.append(current)
    return tuple(pieces)


def wrap_marker_align_log_message(message: str, maximum_units: int) -> tuple[str, ...]:
    """Marker Align 로그 문장을 단어 경계 우선으로 잘리지 않는 여러 줄로 나눕니다."""
    normalized_message = " ".join(str(message).split())
    if not normalized_message:
        return ()
    safe_maximum = max(8, int(maximum_units))
    lines: list[str] = []
    current = ""
    for word in normalized_message.split(" "):
        word_parts = _split_log_token(word, safe_maximum)
        for part_index, part in enumerate(word_parts):
            separator = " " if current and part_index == 0 else ""
            candidate = f"{current}{separator}{part}"
            if current and _text_display_units(candidate) > safe_maximum:
                lines.append(current)
                current = part
            else:
                current = candidate
            if part_index < len(word_parts) - 1:
                lines.append(current)
                current = ""
    if current:
        lines.append(current)
    return tuple(lines)


def _marker_align_log_unit_limit(context) -> int:
    """현재 N 패널 폭과 UI 배율에서 스크롤바를 제외한 표시 단위를 계산합니다."""
    region_width = int(getattr(getattr(context, "region", None), "width", 320) or 320)
    preferences = getattr(context, "preferences", None)
    ui_scale = max(
        0.5,
        float(getattr(getattr(preferences, "system", None), "ui_scale", 1.0) or 1.0),
    )
    available_pixels = max(84.0, float(region_width) - (48.0 * ui_scale))
    return max(12, int(available_pixels / (7.0 * ui_scale)))


_ICP_STAGE_LABELS = {
    "PREPARING_INPUT": "입력 준비",
    "SELECTING_OVERLAP": "겹침 후보 탐색",
    "BUILDING_UNIFORM_SAMPLES": "균일 표본 준비",
    "ESTIMATING_NORMALS_COARSE": "낮은 해상도 노멀 계산",
    "ICP_COARSE_POINT_TO_POINT": "낮은 해상도 Point-to-Point",
    "ESTIMATING_NORMALS_MEDIUM": "중간 해상도 노멀 계산",
    "ICP_MEDIUM_POINT_TO_PLANE": "중간 해상도 Point-to-Plane",
    "ESTIMATING_NORMALS_FINE": "높은 해상도 노멀 계산",
    "ICP_FINE_POINT_TO_PLANE": "높은 해상도 Point-to-Plane",
    "SCALE_REFINEMENT": "스케일 보정",
    "EVALUATING_BEST_CANDIDATE": "최선 후보 평가",
    "COMPLETED_PASS": "PASS 완료",
    "COMPLETED_REVIEW": "REVIEW 완료",
    "FAILED": "실패",
    "CANCELLED": "취소",
    "OPEN3D_PREPARE": "Open3D 입력 준비",
    "OPEN3D_BUILD_FULL_CLOUD": "Open3D 전체 희소점 준비",
    "OPEN3D_GICP_COARSE_DISTANCE": "Open3D GICP 넓은 대응 거리",
    "OPEN3D_GICP_MEDIUM_DISTANCE": "Open3D GICP 중간 대응 거리",
    "OPEN3D_GICP_FINE_DISTANCE": "Open3D GICP 좁은 대응 거리",
    "OPEN3D_ROBUST_POINT_TO_PLANE_COARSE_DISTANCE": "Open3D 강건 Point-to-Plane 넓은 대응 거리",
    "OPEN3D_ROBUST_POINT_TO_PLANE_MEDIUM_DISTANCE": "Open3D 강건 Point-to-Plane 중간 대응 거리",
    "OPEN3D_ROBUST_POINT_TO_PLANE_FINE_DISTANCE": "Open3D 강건 Point-to-Plane 좁은 대응 거리",
    "OPEN3D_EVALUATE": "Open3D 전체점 평가",
    "OPEN3D_COMPLETED_PASS": "Open3D PASS 완료",
    "OPEN3D_COMPLETED_REVIEW": "Open3D REVIEW 완료",
    "SMALL_GICP_PREPARE": "small_gicp 입력 준비",
    "SMALL_GICP_BUILD_FULL_CLOUD": "small_gicp 전체 희소점 준비",
    "SMALL_GICP_GICP_COARSE_DISTANCE": "small_gicp GICP 넓은 대응 거리",
    "SMALL_GICP_GICP_MEDIUM_DISTANCE": "small_gicp GICP 중간 대응 거리",
    "SMALL_GICP_GICP_FINE_DISTANCE": "small_gicp GICP 좁은 대응 거리",
    "SMALL_GICP_EVALUATE": "small_gicp 전체점 평가",
    "SMALL_GICP_SCALE_REFINEMENT": "Refine 균일 스케일 보정",
    "SMALL_GICP_SECOND_GICP_FINE_DISTANCE": "두 번째 small_gicp 정밀 GICP",
    "SMALL_GICP_COMPLETED_PASS": "small_gicp PASS 완료",
    "SMALL_GICP_COMPLETED_REVIEW": "small_gicp REVIEW 완료",
}
_ICP_QUALITY_LABELS = {
    "PASS": "통과",
    "REVIEW": "사용자 확인 필요",
}
_ICP_OBJECTIVE_LABELS = {
    "POINT_TO_POINT": "Point-to-Point",
    "POINT_TO_PLANE": "Point-to-Plane",
}
_ICP_STOP_REASON_LABELS = {
    "NONE": "정상 완료",
    "CONVERGED": "수렴했습니다.",
    "ITERATION_LIMIT": "반복 한도에 도달했습니다.",
    "OBJECTIVE_INCREASE": "목적함수 증가를 감지했습니다.",
    "CORRESPONDENCE_OSCILLATION": "대응점 진동을 감지해 최선 결과를 보존했습니다.",
    "NORMAL_COVERAGE_INSUFFICIENT": "유효 노멀 비율이 부족합니다.",
    "LEVEL_INLIER_DROP": "레벨 전환 후 대응점이 급감해 사용자 검토가 필요합니다.",
    "RADIUS_MINIMUM_REACHED": "최소 대응 반경에 도달했습니다.",
}
_SCALE_REFINE_STATUS_LABELS = {
    "NOT_REQUESTED": "요청 안 함",
    "SKIPPED": "건너뜀",
    "ACCEPTED": "채택",
    "REJECTED": "거부",
}
_ICP_BACKEND_LABELS = {
    "CCCORELIB": "CC 정밀 정렬",
    "CUSTOM_HYBRID": "기존 Hybrid",
    "OPEN3D_GICP": "Open3D GICP",
    "SMALL_GICP": "small_gicp GICP",
    "OPEN3D_ROBUST_POINT_TO_PLANE": "Open3D Robust Point-to-Plane",
}


def _finite_metric_value(value) -> float | None:
    """유한한 품질 수치만 원래 정밀도를 유지한 실수로 반환합니다."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _finite_metric(value, *, precision: int = 4) -> str:
    """유한 수치만 간결한 상태 로그 문자열로 변환합니다."""
    numeric = _finite_metric_value(value)
    if numeric is None:
        return ""
    return f"{numeric:.{precision}g}"


def _nonnegative_count(value) -> int | None:
    """음수가 아닌 정수 진단값만 로그 표시에 사용할 수 있게 반환합니다."""
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 0 else None


def _result_diagnostic(result, field_name: str):
    """런타임 공개 진단 필드가 없거나 수치 변환에 실패해도 패널을 안전하게 유지합니다."""
    try:
        return getattr(result, field_name)
    except (AttributeError, TypeError, ValueError, OverflowError):
        metadata = getattr(result, "metadata", None)
        return metadata.get(field_name) if isinstance(metadata, Mapping) else None


def _metric_pair_text(label: str, metrics: Mapping[str, object] | None) -> str:
    """고정 평가 사전의 Point·Plane RMSE를 한 줄 로그로 구성합니다."""
    if not isinstance(metrics, Mapping):
        return ""
    point_rmse = _finite_metric(metrics.get("point_to_point_rmse"), precision=5)
    plane_rmse = _finite_metric(metrics.get("point_to_plane_rmse"), precision=5)
    parts: list[str] = []
    if point_rmse:
        parts.append(f"Point RMSE {point_rmse}")
    if plane_rmse:
        parts.append(f"Plane RMSE {plane_rmse}")
    return f"{label} · {' · '.join(parts)}" if parts else ""


def _improvement_percent(baseline_value, candidate_value) -> str:
    """동일 평가 표본의 RMSE 두 값에서 부호를 보존한 개선율을 계산합니다."""
    baseline = _finite_metric_value(baseline_value)
    candidate = _finite_metric_value(candidate_value)
    if baseline is None or candidate is None or baseline <= 0.0:
        return ""
    return f"{((baseline - candidate) / max(baseline, 1.0e-12)) * 100.0:+.2f}%"


def _percentage_text(value) -> str:
    """0~1 비율 진단값을 사용자용 백분율 문자열로 변환합니다."""
    numeric = _finite_metric_value(value)
    if numeric is None or numeric < 0.0 or numeric > 1.0:
        return ""
    return f"{numeric * 100.0:.1f}%"


def _icp_progress_entries() -> tuple[str, ...]:
    """worker의 가장 최신 진행 메시지 한 줄만 실행 중 로그로 반환합니다."""
    state = getattr(RUNTIME, "icp_alignment", None)
    progress = getattr(state, "progress", None)
    if progress is None:
        return ("ICP · 입력 준비 중",)
    if str(getattr(progress, "backend_id", "")).strip().upper() == "CCCORELIB":
        message = str(getattr(progress, "message", "") or "").strip()
        return (message or "CC 정렬 · 계산 중",)
    stage = str(getattr(progress, "stage", "")).strip()
    level_name = str(getattr(progress, "level_name", "")).strip()
    iteration = _nonnegative_count(getattr(progress, "iteration", 0)) or 0
    maximum_iterations = _nonnegative_count(getattr(progress, "max_iterations", 0)) or 0
    stage_label = _ICP_STAGE_LABELS.get(stage, "ICP 계산")
    objective_kind = str(getattr(progress, "objective_kind", "") or "").strip().upper()
    objective_label = _ICP_OBJECTIVE_LABELS.get(objective_kind, "")
    detail_parts = [stage_label]
    if level_name and objective_label:
        detail_parts.append(f"{level_name} {objective_label}")
    elif level_name:
        detail_parts.append(level_name)
    elif objective_label:
        detail_parts.append(objective_label)
    if iteration:
        iteration_label = f"{iteration}/{maximum_iterations}" if maximum_iterations else str(iteration)
        detail_parts.append(f"반복 {iteration_label}")
    entries: list[str] = []
    backend_id = str(getattr(progress, "backend_id", "") or "").strip().upper()
    backend_label = _ICP_BACKEND_LABELS.get(backend_id, backend_id)
    open3d_version = str(getattr(progress, "open3d_version", "") or "").strip()
    small_gicp_version = str(getattr(progress, "small_gicp_version", "") or "").strip()
    if backend_label:
        library_version = small_gicp_version if backend_id == "SMALL_GICP" else open3d_version
        suffix = f" {library_version}" if library_version else ""
        entries.append(f"Backend · {backend_label}{suffix}")
    entries.append("상태 · " + " · ".join(detail_parts))
    moving_count = _nonnegative_count(getattr(progress, "moving_sample_count", None))
    reference_count = _nonnegative_count(getattr(progress, "reference_sample_count", None))
    if moving_count is not None or reference_count is not None:
        sample_parts: list[str] = []
        if moving_count is not None:
            sample_parts.append(f"P {moving_count:,}")
        if reference_count is not None:
            sample_parts.append(f"A {reference_count:,}")
        entries.append("표본 · " + " / ".join(sample_parts))
    normal_valid = _nonnegative_count(getattr(progress, "normal_valid_count", None))
    angle_pass = _nonnegative_count(getattr(progress, "normal_angle_pass_count", None))
    if normal_valid is not None or angle_pass is not None:
        normal_parts: list[str] = []
        if normal_valid is not None:
            normal_parts.append(f"유효 {normal_valid:,}")
        if angle_pass is not None:
            normal_parts.append(f"각도 통과 {angle_pass:,}")
        entries.append("노멀 · " + " · ".join(normal_parts))
    maximum_distance = _finite_metric(
        getattr(progress, "maximum_correspondence_distance", None),
        precision=5,
    )
    normal_radius = _finite_metric(getattr(progress, "normal_radius", None), precision=5)
    if maximum_distance or normal_radius:
        radius_parts: list[str] = []
        if maximum_distance:
            radius_parts.append(f"최대 대응 {maximum_distance}")
        if normal_radius:
            radius_parts.append(f"노멀 {normal_radius}")
        entries.append("반경 · " + " · ".join(radius_parts))
    correspondence_count = _nonnegative_count(getattr(progress, "correspondence_count", None))
    point_rmse = _finite_metric(getattr(progress, "point_to_point_rmse", None), precision=5)
    plane_rmse = _finite_metric(getattr(progress, "point_to_plane_rmse", None), precision=5)
    if correspondence_count is not None or point_rmse or plane_rmse:
        metric_parts: list[str] = []
        if plane_rmse:
            metric_parts.append(f"Plane RMSE {plane_rmse}")
        if point_rmse:
            metric_parts.append(f"Point RMSE {point_rmse}")
        if correspondence_count is not None:
            metric_parts.append(f"대응점 {correspondence_count:,}")
        entries.append("현재 · " + " · ".join(metric_parts))
    latest_message = str(getattr(progress, "message", "") or "").strip()
    if latest_message:
        return (latest_message,)
    return (entries[-1] if entries else "ICP · 계산 중",)


def _cccore_metric_entries(label: str, metrics) -> tuple[str, ...]:
    """CC의 독립 겹침 평가와 전체 참고 거리·표본 개수를 구분해 표시합니다."""
    if not isinstance(metrics, Mapping):
        return ()
    distance_parts = []
    distribution_parts = []
    for key, name in (("overlap_mean_rms", "겹침 평가 RMS"), ("overlap_rms", "겹침 합산 RMS")):
        value = _finite_metric(metrics.get(key), precision=5)
        if value:
            distance_parts.append(f"{name} {value}")
    for key, name in (("median", "중앙값"), ("p95", "95백분위 거리")):
        value = _finite_metric(metrics.get(key), precision=5)
        if value:
            distribution_parts.append(f"{name} {value}")
    coverage = metrics.get("correspondence_coverage")
    count = _nonnegative_count(coverage.get("valid_count")) if isinstance(coverage, Mapping) else None
    if count is not None:
        distribution_parts.append(f"주방향 유효 대응 {count:,}개")
    sample_count = _nonnegative_count(metrics.get("evaluation_sample_count"))
    if sample_count is not None:
        distribution_parts.append(f"전체 평가 표본 {sample_count:,}개")
    full = metrics.get("full_reference_metrics")
    if isinstance(full, Mapping):
        full_rms = _finite_metric(full.get("rms"), precision=5)
        if full_rms:
            distribution_parts.append(f"전체 참고 RMS {full_rms}")
    entries = []
    if distance_parts:
        entries.append(f"{label} · " + " · ".join(distance_parts))
    if distribution_parts:
        entries.append(f"{label} 분포 · " + " · ".join(distribution_parts))
    return tuple(entries)


def _cccore_result_entries(result, state) -> tuple[str, ...]:
    """CC 결과의 겹침 평가·표본 진단·추가 배율·검토 사유를 상태 로그에 표시합니다."""
    metadata = getattr(result, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    quality = getattr(result, "quality", {})
    quality = quality if isinstance(quality, Mapping) else {}
    status = str(quality.get("status", metadata.get("quality", ""))).strip().upper()
    entries = ["정렬 엔진 · CC 정밀 정렬", f"상태 · {_ICP_QUALITY_LABELS.get(status, '결과 확인 필요')}"]
    seed = getattr(getattr(state, "input_data", None), "seed", None)
    baseline_label = "마커 초기값" if bool(getattr(seed, "uses_marker_preview", False)) else "초기 위치"
    baseline = metadata.get("baseline_metrics")
    candidate = metadata.get("best_candidate_metrics")
    entries.extend(_cccore_metric_entries(baseline_label, baseline))
    entries.extend(_cccore_metric_entries("최선 결과", candidate))
    if isinstance(baseline, Mapping) and isinstance(candidate, Mapping):
        improvement = _improvement_percent(baseline.get("overlap_mean_rms"), candidate.get("overlap_mean_rms"))
        if improvement:
            entries.append(f"겹침 평가 RMS 개선 · {improvement}")
    sampling = metadata.get("sampling_diagnostics")
    if isinstance(sampling, Mapping):
        registration_count = _nonnegative_count(sampling.get("registration_sample_count"))
        if registration_count is not None:
            entries.append(f"정합 표본 · {registration_count:,}개")
        for field in ("candidate_shortage", "spatial_distribution_warning"):
            if sampling.get(field) is True:
                entries.append("검토 · " + ("겹침 후보가 목표 표본 수보다 부족합니다." if field == "candidate_shortage"
                                           else "겹침 후보의 공간 분포를 확인하십시오."))
    scale = _finite_metric_value(getattr(getattr(result, "icp_delta", None), "uniform_scale", None))
    if scale is not None and scale > 0:
        entries.append(f"추가 균일 스케일 · ×{scale:.7g} ({(scale - 1.0) * 100.0:+.2f}%)")
    reasons = metadata.get("review_reasons", quality.get("reasons", ()))
    if isinstance(reasons, (tuple, list)):
        entries.extend(f"검토 · {reason}" for reason in reasons if isinstance(reason, str) and reason.strip())
    return tuple(entries)


def _icp_result_entries() -> tuple[str, ...]:
    """완료 엔진에 맞는 CC 거리 또는 기존 평가 항목을 구분해 반환합니다."""
    state = getattr(RUNTIME, "icp_alignment", None)
    result = getattr(state, "result", None)
    if result is None:
        return ("ICP 결과를 확인하는 중입니다.",)
    if str(getattr(result, "solver_backend", "")).strip().upper() == "CCCORELIB":
        return _cccore_result_entries(result, state)
    quality = getattr(result, "quality", {})
    if not isinstance(quality, Mapping):
        quality = {}
    entries: list[str] = []
    backend_id = str(
        _result_diagnostic(result, "backend_id")
        or getattr(result, "solver_backend", "")
    ).strip().upper()
    backend_label = _ICP_BACKEND_LABELS.get(backend_id, backend_id)
    open3d_version = str(_result_diagnostic(result, "open3d_version") or "").strip()
    small_gicp_version = str(_result_diagnostic(result, "small_gicp_version") or "").strip()
    if backend_label:
        library_version = small_gicp_version if backend_id == "SMALL_GICP" else open3d_version
        suffix = f" {library_version}" if library_version else ""
        entries.append(f"Backend · {backend_label}{suffix}")
    worker_python = str(_result_diagnostic(result, "worker_python_version") or "").strip()
    worker_architecture = str(_result_diagnostic(result, "platform_architecture") or "").strip()
    if worker_python:
        architecture_suffix = f" · {worker_architecture}" if worker_architecture else ""
        entries.append(f"Worker · Python {worker_python}{architecture_suffix}")
    status = str(_result_diagnostic(result, "quality_status") or "").strip().upper()
    status_label = _ICP_QUALITY_LABELS.get(status, "결과 확인 필요")
    entries.append(f"상태 · {status or 'UNKNOWN'} · {status_label}")
    baseline_metrics = _result_diagnostic(result, "baseline_metrics")
    candidate_metrics = _result_diagnostic(result, "best_candidate_metrics")
    input_data = getattr(state, "input_data", None)
    seed = getattr(input_data, "seed", None)
    baseline_label = "마커 기준" if bool(getattr(seed, "uses_marker_preview", False)) else "초기 위치"
    baseline_line = _metric_pair_text(baseline_label, baseline_metrics)
    candidate_line = _metric_pair_text("ICP 최선", candidate_metrics)
    if baseline_line:
        entries.append(baseline_line)
    if candidate_line:
        entries.append(candidate_line)
    if isinstance(baseline_metrics, Mapping) and isinstance(candidate_metrics, Mapping):
        point_improvement = _improvement_percent(
            baseline_metrics.get("point_to_point_rmse"),
            candidate_metrics.get("point_to_point_rmse"),
        )
        plane_improvement = _improvement_percent(
            baseline_metrics.get("point_to_plane_rmse"),
            candidate_metrics.get("point_to_plane_rmse"),
        )
        improvement_parts: list[str] = []
        if point_improvement:
            improvement_parts.append(f"Point {point_improvement}")
        if plane_improvement:
            improvement_parts.append(f"Plane {plane_improvement}")
        if improvement_parts:
            entries.append("개선 · " + " · ".join(improvement_parts))
    else:
        relative_improvement = _finite_metric_value(
            _result_diagnostic(result, "relative_improvement"),
        )
        if relative_improvement is not None:
            entries.append(f"개선 · Plane {relative_improvement * 100.0:+.2f}%")
    best_level_name = str(_result_diagnostic(result, "best_level_name") or "").strip()
    best_iteration = _nonnegative_count(_result_diagnostic(result, "best_iteration"))
    if best_level_name or best_iteration is not None:
        location_parts: list[str] = []
        if best_level_name:
            location_parts.append(best_level_name)
        if best_iteration is not None:
            location_parts.append(f"{best_iteration}회")
        entries.append("최선 위치 · " + " ".join(location_parts))
    stop_reason = str(_result_diagnostic(result, "solver_stop_reason") or "").strip().upper()
    if stop_reason:
        entries.append(f"종료 · {_ICP_STOP_REASON_LABELS.get(stop_reason, stop_reason)}")
    normal_valid_value = _result_diagnostic(result, "normal_valid_ratio")
    if normal_valid_value is None and isinstance(candidate_metrics, Mapping):
        normal_valid_value = candidate_metrics.get("normal_valid_ratio")
    normal_valid_ratio = _percentage_text(normal_valid_value)
    normal_angle_pass_ratio = _percentage_text(_result_diagnostic(result, "normal_angle_pass_ratio"))
    if normal_valid_ratio or normal_angle_pass_ratio:
        normal_parts: list[str] = []
        if normal_valid_ratio:
            normal_parts.append(f"유효 {normal_valid_ratio}")
        if normal_angle_pass_ratio:
            normal_parts.append(f"각도 통과 {normal_angle_pass_ratio}")
        entries.append("노멀 · " + " · ".join(normal_parts))
    best_candidate = _result_diagnostic(result, "best_candidate")
    maximum_distance = ""
    if isinstance(best_candidate, Mapping):
        maximum_distance = _finite_metric(
            best_candidate.get("maximum_correspondence_distance"),
            precision=5,
        )
    if not maximum_distance and isinstance(candidate_metrics, Mapping):
        maximum_distance = _finite_metric(
            candidate_metrics.get("maximum_correspondence_distance"),
            precision=5,
        )
    if maximum_distance:
        entries.append(f"반경 · 최대 대응 {maximum_distance}")
    density_metrics = _result_diagnostic(result, "density_sampling_metrics")
    if isinstance(density_metrics, Mapping):
        voxel_size = _finite_metric(density_metrics.get("shared_voxel_size"), precision=5)
        occupied_cells = _nonnegative_count(density_metrics.get("occupied_macro_cell_count"))
        macro_cells = _nonnegative_count(density_metrics.get("macro_cell_count"))
        sample_minimum = _nonnegative_count(density_metrics.get("minimum_samples_per_macro_cell"))
        sample_median = _finite_metric(density_metrics.get("median_samples_per_macro_cell"), precision=4)
        sample_maximum = _nonnegative_count(density_metrics.get("maximum_samples_per_macro_cell"))
        density_parts: list[str] = []
        if voxel_size:
            density_parts.append(f"공통 voxel {voxel_size}")
        if occupied_cells is not None and macro_cells is not None:
            density_parts.append(f"macro-cell {occupied_cells}/{macro_cells}")
        if (
            sample_minimum is not None
            or sample_median
            or sample_maximum is not None
        ):
            density_parts.append(
                "표본 "
                + "/".join(
                    part
                    for part in (
                        str(sample_minimum) if sample_minimum is not None else "",
                        sample_median,
                        str(sample_maximum) if sample_maximum is not None else "",
                    )
                    if part
                )
            )
        if density_parts:
            entries.append("균일 표본 · " + " · ".join(density_parts))
    input_policy = str(_result_diagnostic(result, "input_policy") or "").strip()
    overlap_counts = _result_diagnostic(result, "overlap_point_counts")
    if input_policy == "FULL_OVERLAP_NO_DOWNSAMPLING" and isinstance(overlap_counts, Mapping):
        moving_count = _nonnegative_count(overlap_counts.get("moving"))
        reference_count = _nonnegative_count(overlap_counts.get("reference"))
        if moving_count is not None and reference_count is not None:
            entries.append(f"전체 희소점 · P {moving_count:,} / A {reference_count:,}")
    translation = _finite_metric(
        quality.get("delta_translation", _result_diagnostic(result, "delta_translation"))
    )


    rotation = _finite_metric(
        quality.get(
            "delta_rotation_degrees",
            _result_diagnostic(result, "delta_rotation_degrees"),
        )
    )
    motion_parts = []
    if translation:
        motion_parts.append(f"추가 이동 {translation}")
    if rotation:
        motion_parts.append(f"추가 회전 {rotation}°")
    if motion_parts:
        entries.append(" · ".join(motion_parts))
    marker_scale_value = _finite_metric_value(quality.get("marker_uniform_scale"))
    final_scale_value = _finite_metric_value(quality.get("final_uniform_scale"))
    marker_scale = _finite_metric(marker_scale_value, precision=6)
    final_scale = _finite_metric(final_scale_value, precision=6)
    if marker_scale and final_scale:
        if marker_scale_value not in {None, 0.0} and final_scale_value is not None:
            change_percent = (final_scale_value / marker_scale_value - 1.0) * 100.0
            entries.append(
                f"스케일 · {marker_scale} → {final_scale} ({change_percent:+.1f}%)"
            )
        else:
            entries.append(f"스케일 · {marker_scale} → {final_scale}")
    scale_refine_status = str(
        quality.get(
            "scale_refine_status",
            _result_diagnostic(result, "scale_refine_status") or "",
        )
    ).upper()
    if scale_refine_status:
        scale_refine_label = _SCALE_REFINE_STATUS_LABELS.get(
            scale_refine_status,
            scale_refine_status,
        )
        entries.append(f"스케일 보정 · {scale_refine_label}")
    scale_refine_reason = str(quality.get("scale_refine_reason", "")).strip()
    if scale_refine_reason:
        entries.append(f"스케일 보정 · {scale_refine_reason}")
    if status == "REVIEW" or bool(_result_diagnostic(result, "user_review_required")):
        entries.append("안내 · 개선이 미미합니다. 화면에서 비교한 뒤 승인 여부를 결정하십시오.")
    review_reasons = quality.get("review_reasons", ())
    if isinstance(review_reasons, str):
        review_reasons = (review_reasons,)
    if isinstance(review_reasons, (tuple, list)):
        for reason in review_reasons:
            normalized_reason = str(reason).strip()
            if normalized_reason:
                entries.append(f"검토 · {normalized_reason}")
    candidate_metrics = _result_diagnostic(result, "best_candidate_metrics")
    if not isinstance(candidate_metrics, Mapping):
        return ("RMS · 계산 결과 없음",)
    raw_rms = _finite_metric(candidate_metrics.get("point_to_point_rmse"), precision=6)
    weighted_rms = _finite_metric(
        candidate_metrics.get("weighted_point_to_point_rmse"),
        precision=6,
    )
    residual_median = _finite_metric(
        candidate_metrics.get("residual_median"),
        precision=6,
    )
    residual_p90 = _finite_metric(candidate_metrics.get("residual_p90"), precision=6)
    correspondence_count = _nonnegative_count(
        candidate_metrics.get("evaluation_correspondence_count")
    )
    low_weight_count = _nonnegative_count(
        candidate_metrics.get("low_weight_correspondence_count")
    )
    source_isolation_count = _nonnegative_count(
        candidate_metrics.get("source_isolation_candidate_count")
    )
    target_isolation_count = _nonnegative_count(
        candidate_metrics.get("target_isolation_candidate_count")
    )
    distance_unit = str(candidate_metrics.get("distance_unit", "")).strip().upper()
    result_entries = [
        f"Raw RMS · {raw_rms}" if raw_rms else "Raw RMS · 계산 결과 없음",
        (
            f"Weighted RMS · {weighted_rms}"
            if weighted_rms
            else "Weighted RMS · 계산 결과 없음"
        ),
        f"잔차 · 중앙값 {residual_median or '-'} · P90 {residual_p90 or '-'}",
        (
            f"평가 대응점 · {(correspondence_count or 0):,} · "
            f"낮은 가중치 {(low_weight_count or 0):,}"
        ),
        (
            f"고립 후보 점 · P {(source_isolation_count or 0):,} · "
            f"A {(target_isolation_count or 0):,}"
        ),
        (
            "거리 단위 · Project World 좌표 단위"
            if distance_unit == "PROJECT_WORLD_UNIT"
            else f"거리 단위 · {distance_unit or '확인 필요'}"
        ),
    ]
    return tuple(result_entries)


def _draw_project_restore(layout) -> bool:
    """복원 진행 또는 오류가 있을 때 원인·영향·해결과 다음 동작을 표시합니다."""

    state = RUNTIME.project_restore
    phase = str(state.phase)
    if phase == "RESTORING":
        snapshot = state.snapshot
        total = len(getattr(snapshot, "models", ()))
        current = min(total, int(state.current_model_index) + 1) if total else 0
        job = RUNTIME.import_job
        fraction = float(getattr(job, "progress", 0.0) or 0.0)
        if total:
            fraction = (max(0, current - 1) + max(0.0, min(fraction, 1.0))) / total
        box = layout.box()
        box.label(text=f"Project Restore · {current} / {total}", icon="TIME")
        box.progress(factor=max(0.0, min(fraction, 1.0)), type="BAR")
        if job is None:
            box.label(text="현재 단계: 원본 경로 확인", icon="FILE_FOLDER")
        else:
            box.label(text=f"현재 단계: {getattr(job, 'stage', '가져오기 준비')}", icon="IMPORT")
        box.operator("colmap_align.cancel_project_restore", text="복원 취소", icon="CANCEL")
        return True
    if phase not in {"NEEDS_RELINK", "FAILED"}:
        return False
    issue = restore_status_issue()
    box = layout.box()
    box.label(
        text="Project Restore · 원본 필요" if phase == "NEEDS_RELINK" else "Project Restore · 실패",
        icon="ERROR",
    )
    if issue is not None:
        box.label(text=f"원인: {issue.cause}", icon="ERROR")
        box.label(text=f"영향: {issue.impact}", icon="INFO")
        box.label(text=f"해결: {issue.solution}", icon="INFO")
    else:
        box.label(text=state.last_error or "저장된 프로젝트 상태를 복원하지 못했습니다.", icon="ERROR")
    actions = box.row(align=True)
    if phase == "NEEDS_RELINK" and issue is not None and issue.model_id:
        relink = actions.operator(
            "colmap_align.relink_project_source",
            text="원본 다시 연결",
            icon="FILE_FOLDER",
        )
        relink.model_id = issue.model_id
    actions.operator("colmap_align.retry_project_restore", text="재시도", icon="FILE_REFRESH")
    box.operator("colmap_align.start_new_project_after_restore_error", text="새 프로젝트 시작", icon="NEWFOLDER")
    return True


def _icp_status_entries() -> tuple[str, ...]:
    """ICP phase별 실행·결과·실패 상태를 Marker Align 로그에 통합합니다."""
    if is_icp_running():
        return _icp_progress_entries()
    if is_icp_result_preview_active():
        return _icp_result_entries()
    state = getattr(RUNTIME, "icp_alignment", None)
    last_error = str(getattr(state, "last_error", "")).strip()
    if last_error:
        return (f"ICP · {last_error}",)
    return ("ICP · 실행 준비 완료",)


def _icp_progress_fraction() -> float:
    """패널 progress bar에 쓸 안전한 0~1 ICP 진행률을 반환합니다."""
    progress = getattr(getattr(RUNTIME, "icp_alignment", None), "progress", None)
    numeric = _finite_metric_value(getattr(progress, "progress", 0.0))
    if numeric is None:
        return 0.0
    return max(0.0, min(numeric, 1.0))


def marker_align_status_entries(
    preview_active: bool,
    start_reason: str | None,
    direct_align_ready: bool = False,
) -> tuple[str, ...]:
    """현재 작업의 최신 상태 또는 완료 ICP 평가 항목을 구성합니다."""
    if is_icp_running() or is_icp_result_preview_active():
        return _icp_status_entries()
    icp_state = getattr(RUNTIME, "icp_alignment", None)
    last_error = str(getattr(icp_state, "last_error", "") or "").strip()
    if last_error:
        return (f"ICP · {last_error}",)
    if preview_active and RUNTIME.marker_alignment.result is not None:
        result = RUNTIME.marker_alignment.result
        metrics = result.metrics
        entries = (f"RMS · {metrics.rms:.6g}",)
    elif direct_align_ready:
        reference_count = len(RUNTIME.model_catalog.active_models())
        entries = (f"직접 Align · A 그룹 전체 {reference_count}개를 기준으로 현재 P 위치를 초기값으로 사용합니다.",)
    else:
        entries = (start_reason or "마커 입력 · Marker Align 미리보기 준비 완료",)
    return entries


def wrapped_marker_align_status_entries(
    context,
    preview_active: bool,
    start_reason: str | None,
    direct_align_ready: bool = False,
) -> tuple[str, ...]:
    """최신 상태 메시지를 상태 로그 목록 형식으로 반환합니다."""
    del context
    return marker_align_status_entries(
        preview_active,
        start_reason,
        direct_align_ready,
    )


def _indented_panel_body(layout):
    """메뉴 제목보다 본문을 반 UI 단위 오른쪽에 배치합니다."""
    row = layout.row()
    gutter = row.column()
    gutter.ui_units_x = 0.5
    gutter.label(text="")
    return row.column()


class COLMAPALIGN_PT_main(bpy.types.Panel):
    """COLMAP Align 작업공간의 재연결 상태를 표시합니다."""

    bl_idname = "COLMAPALIGN_PT_main"
    bl_label = "COLMAP Align"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CONTROL_PANEL_CATEGORY
    bl_parent_id = ""
    bl_order = 0
    bl_options = {"HIDE_HEADER"}

    @classmethod
    def poll(cls, context):
        """전용 작업공간의 오른쪽 P 뷰에서만 부모 패널을 표시합니다."""
        return _moving_panel_poll(context)

    def draw(self, context):
        """재연결 중에는 상태 안내만 표시하고 수동 종료 동작은 제공하지 않습니다."""

        if not is_session_active_for_window(context.window):
            error_message = str(RUNTIME.workspace_setup_error or "").strip()
            _indented_panel_body(self.layout).label(
                text=error_message or "Align 세션 연결 중",
                icon="ERROR" if error_message else "TIME",
            )


def _draw_data_layer_row(layout, model, catalog, *, importing: bool, selection_locked: bool) -> None:
    """클릭 대상 ID와 내장 OBJ 면 수를 각 데이터셋 한 행에 표시합니다."""
    is_active = model.registration_status == MODEL_STATUS_REGISTERED
    is_selected = model.model_id in catalog.selected_model_ids
    is_colmap = getattr(model, "geometry_kind", "COLMAP_POINT_CLOUD") == "COLMAP_POINT_CLOUD"
    surface_model = surface_model_data_for_id(model.model_id)
    embedded_status = ""
    if bool(getattr(surface_model, "embedded_asset_uuid", "")):
        embedded_status = (
            f" · OBJ 내장됨 · 원본 면 {int(surface_model.original_polygon_count):,}"
            f" / 삼각면 {int(surface_model.proxy_triangle_count):,}"
        )
    row = layout.row(align=True)
    row.enabled = not importing
    name_row = row.row(align=True)
    name_row.enabled = not selection_locked
    name_row.active = is_active
    operator = name_row.operator(
        "colmap_align.select_data_layer",
        text=f"{model.display_name} · {'COLMAP' if is_colmap else 'OBJ'}{embedded_status}",
        icon="PINNED" if is_active else "BLANK1",
        emboss=is_selected,
        depress=is_selected,
    )
    operator.model_id = model.model_id
    points_row = row.row(align=True)
    points_row.enabled = is_colmap
    points = points_row.operator(
        "colmap_align.toggle_data_layer_geometry",
        text="",
        icon="POINTCLOUD_DATA",
        depress=is_colmap and model.show_points,
    )
    points.model_id = model.model_id
    points.geometry_kind = "COLMAP_POINT_CLOUD"
    mesh_row = row.row(align=True)
    mesh_row.enabled = surface_model is not None
    mesh = mesh_row.operator(
        "colmap_align.toggle_data_layer_geometry",
        text="",
        icon="MESH_DATA",
        depress=mesh_row.enabled and model.show_surface,
    )
    mesh.model_id = model.model_id
    mesh.geometry_kind = "TRIANGLE_MESH"
    axes_row = row.row(align=True)
    axes_row.enabled = axis_edit_block_reason(RUNTIME, model.model_id) is None
    axes = axes_row.operator("colmap_align.edit_geometry_axes", text="", icon="PREFERENCES")
    axes.model_id = model.model_id
    visibility = row.operator(
        "colmap_align.toggle_data_layer_visibility",
        text="",
        icon="HIDE_OFF" if model.is_visible else "HIDE_ON",
        emboss=False,
    )
    visibility.model_id = model.model_id


def _draw_marker_target_visibility(layout, catalog) -> None:
    """형상 고정·충돌과 숨겨진 P 대상 안내를 버튼 아래 상태 영역에 표시합니다."""
    state = RUNTIME.marker_selection
    message = state.geometry_status_message()
    if message:
        layout.label(text=message, icon="ERROR" if state.geometry_conflict_message() else "INFO")
    reason = state.draft_geometry_rejection_message() if state.has_draft else None
    if reason and reason != message:
        layout.label(text=reason, icon="ERROR")
    moving = catalog.selected_inactive_model
    if moving is not None and not moving.is_visible:
        layout.label(text=f"{moving.display_name}: 마커를 지정하려면 레이어를 표시하십시오.", icon="INFO")


class COLMAPALIGN_PT_data_layers(bpy.types.Panel):
    """Runtime 카탈로그를 단순 Data Layers 목록으로 표시합니다."""

    bl_idname = "COLMAPALIGN_PT_data_layers"
    bl_label = "Data Layers"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CONTROL_PANEL_CATEGORY
    bl_parent_id = ""
    bl_order = 10

    @classmethod
    def poll(cls, context):
        """전용 작업공간의 오른쪽 데이터 레이어 뷰에서만 패널을 표시합니다."""
        return _moving_panel_poll(context)

    def draw(self, context):
        """데이터셋마다 한 행에 선택·점군·메시·축 설정·전체 표시를 그립니다."""
        layout = _indented_panel_body(self.layout)
        _set_session_layout_enabled(layout, context)
        if _draw_project_restore(layout):
            return
        catalog = RUNTIME.model_catalog
        job = RUNTIME.import_job
        preview_active = is_marker_preview_active()
        icp_locked = is_icp_input_locked()
        merge_locked = is_merge_export_input_locked()
        obj_locked = is_obj_export_input_locked()
        export_locked = merge_locked or obj_locked
        axes_locked = axis_edit_active_block_reason(RUNTIME) is not None
        if job is None:
            button_text = "COLMAP 모델 불러오기" if not catalog.models else "COLMAP 모델 추가"
            import_row = layout.row()
            import_row.enabled = not preview_active and not icp_locked and not export_locked and not axes_locked
            import_row.operator("colmap_align.import_model", text=button_text, icon="IMPORT")
            import_row.operator("colmap_align.import_obj", text="OBJ 추가", icon="MESH_DATA")
            if axes_locked:
                layout.label(text="축 설정을 적용하거나 취소한 뒤 레이어를 변경하십시오.", icon="LOCKED")
            elif icp_locked:
                layout.label(text="ICP 실행 또는 결과 미리보기 중에는 가져오기와 레이어 변경이 잠깁니다.", icon="LOCKED")
            elif preview_active:
                layout.label(text="마커 미리보기 중에는 가져오기와 레이어 변경이 잠깁니다.", icon="LOCKED")
            elif merge_locked:
                layout.label(text="구조 통합 내보내기 중에는 가져오기와 레이어 변경이 잠깁니다.", icon="LOCKED")
            elif obj_locked:
                layout.label(text="OBJ 내보내기 중에는 가져오기와 레이어 변경이 잠깁니다.", icon="LOCKED")
            if not catalog.models:
                layout.label(text="불러온 데이터 레이어가 없습니다.", icon="INFO")
                return
        else:
            status = import_status_view(job)
            layout.label(text="COLMAP 모델 불러오는 중", icon="TIME")
            layout.progress(
                factor=status.fraction,
                type="BAR",
                text=status.percentage_text,
            )
            progress_column = layout.column(align=True)
            progress_column.label(text=f"단계: {status.stage_text}")
            if status.detail_stage_text:
                progress_column.label(text=f"세부 작업: {status.detail_stage_text}")
            progress_column.label(text=f"처리량: {status.processed_text}")
            progress_column.label(text=f"경과 시간: {status.elapsed_text}")

            layout.separator()
            source_column = layout.column(align=True)
            source_column.label(text=f"선택 폴더: {status.dataset_text}", icon="FILE_FOLDER")
            if status.source_text:
                source_column.label(text=f"모델 폴더: {status.source_text}")
            source_column.label(text=f"입력 형식: {status.format_text}")
            source_column.label(text=f"표시 축: {status.axis_text}")
            layout.operator("colmap_align.cancel_import_model", text="가져오기 취소", icon="CANCEL")
        for model in (*catalog.active_models(), *catalog.inactive_models()):
            _draw_data_layer_row(
                layout,
                model,
                catalog,
                importing=job is not None,
                selection_locked=preview_active or icp_locked or export_locked or axes_locked,
            )



class COLMAPALIGN_PT_viewport(bpy.types.Panel):
    """점 크기·변환 기즈모·임시 색상 표시 설정을 제공합니다."""

    bl_idname = "COLMAPALIGN_PT_viewport"
    bl_label = "Viewport"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CONTROL_PANEL_CATEGORY
    bl_parent_id = ""
    bl_order = 20

    @classmethod
    def poll(cls, context):
        """전용 작업공간의 오른쪽 데이터 레이어 뷰에서만 패널을 표시합니다."""
        return _moving_panel_poll(context)

    def draw(self, context):
        """Data Layers와 독립적인 COLMAP 뷰포트 표시 설정을 그립니다."""
        layout = _indented_panel_body(self.layout)
        _configure_layout(layout)
        _set_session_layout_enabled(layout, context)
        settings = context.scene.colmap_align_settings
        point_size_row = layout.row()
        point_size_row.use_property_split = False
        point_size_row.alignment = "EXPAND"
        point_size_row.prop(settings, "point_size")
        gizmo_row = layout.row()
        gizmo_row.use_property_split = False
        gizmo_row.alignment = "LEFT"
        gizmo_row.label(text="", icon="BLANK1")
        gizmo_row.prop(settings, "show_transform_gizmo")
        color_row = layout.row()
        color_row.use_property_split = False
        color_row.alignment = "LEFT"
        color_row.label(text="", icon="BLANK1")
        color_row.prop(settings, "use_colmap_rgb")


class COLMAPALIGN_PT_marker(bpy.types.Panel):
    """확정 목록 선택, 드래프트 편집과 세션 전용 Undo를 제공합니다."""

    bl_idname = "COLMAPALIGN_PT_marker"
    bl_label = "Marker Align"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CONTROL_PANEL_CATEGORY
    bl_parent_id = ""
    bl_order = 30

    @classmethod
    def poll(cls, context):
        """전용 작업공간의 오른쪽 데이터 레이어 뷰에서만 마커 패널을 표시합니다."""
        return _moving_panel_poll(context)

    def draw(self, context):
        """마커 버튼의 행 수를 유지하고 형상·편집 안내는 하단 상태 영역에 표시합니다."""
        layout = _indented_panel_body(self.layout)
        _configure_layout(layout)
        _set_session_layout_enabled(layout, context)
        restore_reason = restore_input_block_reason()
        if restore_reason is not None:
            layout.label(text=restore_reason, icon="LOCKED")
            return
        state = RUNTIME.marker_selection
        journal = RUNTIME.undo_journal
        catalog = RUNTIME.model_catalog
        preview_active = is_marker_preview_active()
        icp_locked = is_icp_input_locked()
        merge_locked = is_merge_export_input_locked()
        obj_locked = is_obj_export_input_locked()
        export_locked = merge_locked or obj_locked
        editing_layout = layout.column()
        editing_layout.enabled = (
            not RUNTIME.import_in_progress
            and not preview_active
            and not icp_locked
            and not export_locked
            and axis_edit_active_block_reason(RUNTIME) is None
        )
        pair_list = marker_pair_list(context.window_manager)
        if pair_list is not None:
            editing_layout.template_list(
                "COLMAPALIGN_UL_marker_pairs",
                "",
                pair_list,
                "items",
                pair_list,
                "active_index",
                rows=4,
            )

        if state.has_draft:
            confirm_row = editing_layout.row(align=True)
            confirm_row.enabled = state.can_confirm_draft()
            confirm_row.operator("colmap_align.confirm_marker_selection", text="쌍 지정", icon="CHECKMARK")
            cancel_row = editing_layout.row(align=True)
            cancel_row.enabled = RUNTIME.marker_command != MARKER_COMMAND_CANCEL
            cancel_row.operator("colmap_align.cancel_marker_selection", text="선택 취소", icon="X")
        else:
            add_row = editing_layout.row()
            add_row.enabled = (
                not RUNTIME.modal_active
                and catalog.selected_inactive_model is not None
                and catalog.selected_inactive_model.is_visible
            )
            add_row.operator("colmap_align.add_marker_pair", text="마커 쌍 추가", icon="ADD")
            selected_pair = state.active_pair
            actions = editing_layout.column(align=True)
            actions.enabled = selected_pair is not None and not RUNTIME.modal_active
            row = actions.row(align=True)
            row.operator("colmap_align.edit_marker_pair", text="마커 편집", icon="GREASEPENCIL")
            row.operator("colmap_align.delete_marker_pair", text="선택 쌍 삭제", icon="TRASH")

        history_row = editing_layout.row(align=True)
        undo_column = history_row.column(align=True)
        undo_column.enabled = journal.can_undo_draft if journal.scope_active else journal.can_undo
        undo_column.operator("colmap_align.undo_marker_selection", text="되돌리기", icon="LOOP_BACK")
        redo_column = history_row.column(align=True)
        redo_column.enabled = journal.can_redo_draft if journal.scope_active else journal.can_redo
        redo_column.operator("colmap_align.redo_marker_selection", text="다시 실행", icon="LOOP_FORWARDS")

        layout.separator()
        preview_box = layout.box()
        preview_box.label(text="Marker Align", icon="ORIENTATION_GIMBAL")
        start_reason = marker_preview_start_block_reason()
        direct_align_reason = direct_align_start_block_reason()
        direct_align_ready = direct_align_reason is None
        align_reason = align_start_block_reason(context)
        align_ready = align_reason is None
        settings = context.scene.colmap_align_settings
        phase = icp_phase()
        marker_scale_action = preview_box.row()
        marker_scale_action.use_property_split = False
        marker_scale_action.alignment = "LEFT"
        marker_scale_action.enabled = not preview_active and not icp_locked and not export_locked
        marker_scale_action.prop(
            settings,
            "marker_use_uniform_scale",
            text="Marker Refine Scale",
        )
        if is_icp_running():
            marker_action_row = preview_box.row()
            marker_action_row.enabled = False
            marker_action_row.operator(
                "colmap_align.toggle_marker_preview",
                text=marker_preview_button_label(),
                icon="CANCEL",
            )
            align_action = preview_box.row()
            align_action.enabled = (
                not export_locked and phase != ICP_ALIGNMENT_PHASE_CANCEL_PENDING
            )
            align_action.operator("colmap_align.cancel_icp", text="ICP 취소", icon="CANCEL")
        elif is_icp_result_preview_active():
            result_action_row = preview_box.row(align=True)
            result_action_row.enabled = not export_locked
            discard_action = result_action_row.column(align=True)
            discard_action.operator(
                "colmap_align.discard_icp_preview",
                text="결과 버리기",
                icon="TRASH",
            )
            approve_action = result_action_row.column(align=True)
            approve_action.enabled = is_icp_result_approval_available()
            approve_action.operator(
                "colmap_align.approve_icp_result",
                text="정렬 승인",
                icon="CHECKMARK",
            )
        else:
            marker_action_row = preview_box.row()
            marker_action_row.enabled = not export_locked and (preview_active or start_reason is None)
            marker_action_row.operator(
                "colmap_align.toggle_marker_preview",
                text=marker_preview_button_label(),
                icon="CANCEL" if preview_active else "HIDE_OFF",
            )
            align_action = preview_box.row()
            align_action.enabled = align_ready and not icp_locked and not export_locked
            align_action.operator("colmap_align.align_marker_preview", text="Align", icon="PLAY")

        if preview_active or direct_align_ready:
            preview_box.label(
                text=geometry_mode_summary(RUNTIME) + " · 숨긴 A 포함",
                icon="OUTLINER_OB_GROUP_INSTANCE",
            )

        if is_icp_running():
            progress_value = _icp_progress_fraction()
            preview_box.progress(
                factor=progress_value,
                type="BAR",
                text=f"{int(round(progress_value * 100.0))}%",
            )

        if merge_locked:
            preview_box.label(text="구조 통합 내보내기 중에는 마커·ICP 입력이 잠깁니다.", icon="LOCKED")
        elif obj_locked:
            preview_box.label(text="OBJ 내보내기 중에는 마커·ICP 입력이 잠깁니다.", icon="LOCKED")

        preview_box.label(text="상태 로그")
        _draw_marker_target_visibility(preview_box, catalog)
        status_start_reason = start_reason
        status_direct_align_ready = direct_align_ready
        if direct_align_ready and not preview_active:
            status_start_reason = align_reason or (
                f"자동 정렬 · A 그룹 전체 {len(catalog.active_models())}개 기준 · "
                f"{automatic_alignment_description()}"
            )
            status_direct_align_ready = False
        log_entries = wrapped_marker_align_status_entries(
            context,
            preview_active,
            status_start_reason,
            status_direct_align_ready,
        )
        if state.has_draft:
            draft = state.draft
            log_entries = (
                f"편집 중 A{draft.pair_id} · P{draft.pair_id}",
                "편집 중에는 목록 편집/삭제 잠김",
                *log_entries,
            )
        sync_marker_align_log(log_entries, context.window_manager)
        log_list = marker_align_log_list(context.window_manager)
        if log_list is not None:
            preview_box.template_list(
                "COLMAPALIGN_UL_marker_align_log",
                "",
                log_list,
                "items",
                log_list,
                "active_index",
                rows=max(1, min(6, len(log_entries))),
            )


class COLMAPALIGN_PT_merge_export(bpy.types.Panel):
    """A 그룹 전체를 파일 탐색기 한 번으로 구조 통합해 내보냅니다."""

    bl_idname = "COLMAPALIGN_PT_merge_export"
    bl_label = "Merge & Export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CONTROL_PANEL_CATEGORY
    bl_parent_id = ""
    bl_order = 40

    @classmethod
    def poll(cls, context):
        """전용 작업공간의 오른쪽 데이터 레이어 뷰에서만 패널을 표시합니다."""

        return _moving_panel_poll(context)

    def draw(self, context):
        """COLMAP과 OBJ Export를 분리하고 실행 중인 작업의 진행·결과를 표시합니다."""

        layout = _indented_panel_body(self.layout)
        _configure_layout(layout)
        _set_session_layout_enabled(layout, context)
        restore_reason = restore_input_block_reason()
        if restore_reason is not None:
            layout.label(text=restore_reason, icon="LOCKED")
            return
        merge_state = RUNTIME.merge_export
        obj_state = RUNTIME.obj_export

        if is_merge_export_running():
            progress = getattr(merge_state, "progress", None)
            fraction = float(getattr(progress, "fraction", 0.0) or 0.0)
            fraction = min(1.0, max(0.0, fraction))
            layout.progress(
                factor=fraction,
                type="BAR",
                text=f"통합 COLMAP 내보내기 {int(round(fraction * 100.0))}%",
            )
            for entry in merge_export_status_entries():
                layout.label(text=entry)
            cancel_row = layout.row()
            cancel_row.enabled = merge_export_phase() != "CANCEL_PENDING"
            cancel_row.operator("colmap_align.cancel_merge_export", text="작업 취소", icon="CANCEL")
            return

        if is_obj_export_running():
            progress = getattr(obj_state, "progress", None)
            fraction = float(getattr(progress, "fraction", 0.0) or 0.0)
            fraction = min(1.0, max(0.0, fraction))
            layout.progress(
                factor=fraction,
                type="BAR",
                text=f"OBJ 내보내기 {int(round(fraction * 100.0))}%",
            )
            for entry in obj_export_status_entries():
                layout.label(text=entry)
            cancel_row = layout.row()
            cancel_row.enabled = obj_export_phase() != "CANCEL_PENDING"
            cancel_row.operator("colmap_align.cancel_obj_export", text="작업 취소", icon="CANCEL")
            return

        merge_reason = merge_export_start_block_reason(require_output_path=False)
        merge_column = layout.column(align=True)
        merge_column.enabled = merge_reason is None
        merge_column.operator("colmap_align.merge_export", text="COLMAP Export", icon="EXPORT")
        if merge_reason is not None:
            layout.label(text=merge_reason, icon="INFO")

        obj_reason = obj_export_start_block_reason()
        obj_column = layout.column(align=True)
        obj_column.enabled = obj_reason is None
        obj_column.operator("colmap_align.obj_export", text="OBJ Export", icon="MESH_DATA")
        if obj_reason is not None:
            layout.label(text=obj_reason, icon="INFO")

        merge_error = str(getattr(merge_state, "last_error", "")).strip()
        if merge_error:
            error_box = layout.box()
            error_box.label(text="COLMAP Export 오류", icon="ERROR")
            issue = getattr(merge_state, "last_issue", None)
            if issue is not None:
                error_box.label(text=f"원인: {issue.cause}")
                error_box.label(text=f"영향: {issue.impact}")
                error_box.label(text=f"해결: {issue.remedy}")
            else:
                for entry in format_merge_export_error(merge_error):
                    error_box.label(text=entry)

        if merge_export_phase() == "COMPLETED":
            complete_box = layout.box()
            complete_box.label(text="COLMAP Export 완료", icon="CHECKMARK")
            complete_box.label(text=f"출력 · {merge_state.last_success_output_root}")
            result_counts = getattr(getattr(merge_state, "result", None), "counts", {})
            if isinstance(result_counts, Mapping):
                for field_name, value in result_counts.items():
                    complete_box.label(text=f"{field_name} · {int(value):,}")
            complete_row = complete_box.row(align=True)
            complete_row.operator("colmap_align.open_merge_export_output", text="폴더 열기", icon="FILE_FOLDER")
            complete_row.operator("colmap_align.open_merge_export_report", text="보고서 보기", icon="TEXT")

        obj_error = str(getattr(obj_state, "last_error", "")).strip()
        if obj_error:
            error_box = layout.box()
            error_box.label(text="OBJ Export 오류", icon="ERROR")
            error_box.label(text=obj_error)

        if obj_export_phase() == "COMPLETED":
            complete_box = layout.box()
            complete_box.label(text="OBJ Export 완료", icon="CHECKMARK")
            complete_box.label(text=f"출력 · {obj_state.last_success_output_root}")
            complete_box.label(text=f"정점 · {int(obj_state.last_vertex_count):,}")
            complete_box.label(text=f"삼각면 · {int(obj_state.last_face_count):,}")
            complete_row = complete_box.row(align=True)
            complete_row.operator("colmap_align.open_obj_export_output", text="폴더 열기", icon="FILE_FOLDER")
            complete_row.operator("colmap_align.open_obj_export_report", text="보고서 보기", icon="TEXT")


PANEL_CLASSES = (
    COLMAPALIGN_PT_main,
    COLMAPALIGN_PT_data_layers,
    COLMAPALIGN_PT_viewport,
    COLMAPALIGN_PT_marker,
    COLMAPALIGN_PT_merge_export,
)

CLASSES = (
    COLMAPALIGN_UL_marker_pairs,
    COLMAPALIGN_UL_marker_align_log,
) + PANEL_CLASSES
