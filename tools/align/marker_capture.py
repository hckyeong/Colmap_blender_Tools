"""마커 입력 동안 대상 표시와 원본·축·정렬 기준을 불변 스냅샷으로 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_GEOMETRY_SOURCE_COMPANION,
    MARKER_GEOMETRY_SOURCE_MAIN,
    ROLE_MOVING,
    ROLE_REFERENCE,
)


@dataclass(frozen=True)
class MarkerCaptureGeometry:
    """입력 가능한 한 표시 형상의 출처와 원본·변환 지문을 보관합니다."""

    role: str
    model_id: str
    geometry_source: str
    geometry_kind: str
    geometry_identity: int
    geometry_revision: int
    source_fingerprint: str
    axis_fingerprint: str
    alignment_fingerprint: str


@dataclass(frozen=True)
class MarkerCaptureSnapshot:
    """한 드래프트가 고정한 A 전체와 선택 P의 표시 및 입력 후보를 보관합니다."""

    layer_signatures: tuple[tuple, ...]
    selected_moving_model_ids: tuple[str, ...]
    geometries: tuple[MarkerCaptureGeometry, ...]

    def role_geometry_kinds(self, role: str) -> frozenset[str]:
        """지정 역할의 보이는 후보 종류를 중복 없이 반환합니다."""
        return frozenset(item.geometry_kind for item in self.geometries if item.role == role)


def marker_geometry_kind_label(geometry_kind: str | None) -> str:
    """마커 안내에서 사용하는 형상 종류의 짧은 한글 이름을 반환합니다."""
    return "점군" if geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD else "메시" if geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH else "종류 선택 대기"


def _capture_snapshot(runtime: Any) -> MarkerCaptureSnapshot:
    """현재 대상 레이어와 실제로 보이는 형상의 작은 지문만 복사합니다."""
    from .geometry_access import geometry_kind_for_model
    from .marker_geometry import resolve_marker_geometry

    catalog = runtime.model_catalog
    repository = runtime.model_repository
    moving = catalog.selected_inactive_model
    if moving is None:
        raise ValueError("마커를 지정할 P 레이어를 하나 선택하십시오.")
    records = [(ROLE_REFERENCE, record) for record in catalog.active_models()]
    records.append((ROLE_MOVING, moving))
    signatures = []
    geometries = []
    for role, record in records:
        signatures.append((role, record.model_id, record.registration_status,
                           record.is_visible, record.show_points, record.show_surface))
        if not record.is_visible:
            continue
        if bool(record.show_points) == bool(record.show_surface):
            raise ValueError(f"{record.display_name}에서 점군 또는 메시 하나를 선택하십시오.")
        if record.show_points:
            if record.geometry_kind != GEOMETRY_KIND_COLMAP_POINT_CLOUD:
                raise ValueError(f"{record.display_name}의 점군 표시가 유효하지 않습니다.")
            source = MARKER_GEOMETRY_SOURCE_MAIN
        else:
            source = (MARKER_GEOMETRY_SOURCE_MAIN
                      if record.geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH
                      else MARKER_GEOMETRY_SOURCE_COMPANION)
        resolved = resolve_marker_geometry(repository, record.model_id, source)
        model = resolved.model_data
        geometries.append(MarkerCaptureGeometry(
            role=role, model_id=record.model_id, geometry_source=source,
            geometry_kind=geometry_kind_for_model(model), geometry_identity=id(model),
            geometry_revision=int(getattr(model, "geometry_revision", 0)),
            source_fingerprint=str(resolved.source_fingerprint),
            axis_fingerprint=str(resolved.display_transform.fingerprint),
            alignment_fingerprint=str(repository.alignment_transform_for_model_id(record.model_id).fingerprint),
        ))
    return MarkerCaptureSnapshot(tuple(signatures), tuple(catalog.selected_moving_model_ids), tuple(geometries))


def create_marker_capture_snapshot(runtime: Any) -> MarkerCaptureSnapshot:
    """기존 역할 종류와 일치하는 보이는 A/P 후보가 있을 때 입력 기준을 만듭니다."""
    selection = runtime.marker_selection
    conflict = selection.geometry_conflict_message()
    if conflict is not None:
        raise ValueError(conflict)
    snapshot = _capture_snapshot(runtime)
    for role, label in ((ROLE_REFERENCE, "A"), (ROLE_MOVING, "P")):
        kinds = snapshot.role_geometry_kinds(role)
        locked = selection.role_geometry_kind(role)
        if not kinds:
            raise ValueError(f"마커를 지정할 {label} 레이어의 형상을 표시하십시오.")
        if locked is not None and locked not in kinds:
            raise ValueError(f"{label} 마커는 {marker_geometry_kind_label(locked)}로 고정되어 있습니다. 해당 형상을 표시한 뒤 입력을 시작하십시오.")
    return snapshot


def marker_capture_validation_message(runtime: Any, snapshot: MarkerCaptureSnapshot) -> str | None:
    """입력 시작 뒤 대상 표시·소속·원본·축·정렬이 바뀌었는지 검사합니다."""
    try:
        current = _capture_snapshot(runtime)
    except (ValueError, RuntimeError, KeyError, AttributeError):
        return "마커 입력 중 대상 표시·원본·축 또는 정렬 기준이 바뀌었습니다. 입력을 취소한 뒤 다시 시작하십시오."
    if current != snapshot:
        return "마커 입력 중 대상 표시·원본·축 또는 정렬 기준이 바뀌었습니다. 입력을 취소한 뒤 다시 시작하십시오."
    return None


def marker_capture_change_block_reason(runtime: Any, model_id: str | None = None) -> str | None:
    """입력 대상 레이어의 표시 변경을 시작 전에 차단할 안내를 반환합니다."""
    selection = runtime.marker_selection
    if not selection.has_draft:
        return None
    snapshot = getattr(selection.draft, "capture_snapshot", None)
    if model_id is not None and snapshot is not None:
        if model_id not in {item[1] for item in snapshot.layer_signatures}:
            return None
    return "마커 입력을 확정하거나 취소한 뒤 대상 레이어의 표시와 A/P 소속을 변경하십시오."
