"""선택한 점군·메시를 CCCoreLib용 공통 좌표와 불변 원본 지문으로 고정합니다."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any

import numpy as np

from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_GEOMETRY_SOURCE_COMPANION,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_POINT_KIND_COLMAP,
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KIND_MESH_VERTEX,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from ..geometry_access import geometry_kind_for_model
from ..marker_geometry import marker_geometry_model_for_source
from .icp_mesh_cleanup import prepare_mesh_alignment_geometry
from .types import Sim3Transform


_BLOCK_ROWS = 65_536
_SCHEMA = "CCCORE_GEOMETRY_V1"


class CcCoreGeometryError(ValueError):
    """선택 형상이나 원본 좌표가 정렬 입력 계약을 만족하지 않을 때 발생합니다."""


def _digest_value(value: object) -> str:
    """불변 값의 정렬된 UTF-8 표현으로 결정적인 입력 지문을 계산합니다."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _matrix_tuple(value: object) -> tuple[tuple[float, ...], ...]:
    """기존 Sim(3) 검증을 통과한 작은 행렬을 저장 가능한 불변 값으로 바꿉니다."""
    matrix = Sim3Transform(matrix_float64=value).matrix_float64
    return tuple(tuple(float(component) for component in row) for row in matrix)


def _immutable_array(value: np.ndarray, dtype: str) -> np.ndarray:
    """원본을 변경하지 않고 쓰기 권한을 다시 켤 수 없는 배열을 반환합니다."""
    array = np.asarray(value, dtype=np.dtype(dtype), order="C")
    base = array
    while isinstance(base, np.ndarray) and base.base is not None:
        base = base.base
    if isinstance(base, bytes) and not array.flags.writeable:
        return array
    return np.frombuffer(array.tobytes(order="C"), dtype=np.dtype(dtype)).reshape(array.shape)


@dataclass(frozen=True, slots=True)
class GeometrySourceProvenance:
    """형상 한 슬롯의 원본·배열·축·공통 정렬을 순수 값으로 기록합니다."""

    owner_model_id: str
    geometry_kind: str
    geometry_source: str
    geometry_revision: int
    source_path: str
    source_fingerprint: str
    positions_fingerprint: str
    triangles_fingerprint: str
    point_count: int
    triangle_count: int
    display_transform_fingerprint: str
    display_transform_float64: tuple[tuple[float, ...], ...]
    committed_alignment_fingerprint: str
    committed_alignment_float64: tuple[tuple[float, ...], ...]
    applied_alignment_float64: tuple[tuple[float, ...], ...]

    def to_mapping(self) -> dict[str, Any]:
        """배열과 런타임 포인터가 없는 원본 감사 기록을 반환합니다."""
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def without_content_hashes(self) -> GeometrySourceProvenance:
        """반복 상태 검사에서 대형 배열 해시만 제외한 작은 기록을 반환합니다."""
        return replace(self, positions_fingerprint="", triangles_fingerprint="")


@dataclass(frozen=True, slots=True)
class GeometryProvenance:
    """단일 또는 결합 형상에 기여한 소유자별 원본 출처를 보관합니다."""

    sources: tuple[GeometrySourceProvenance, ...]

    def to_mapping(self) -> dict[str, Any]:
        """원본별 감사 기록을 외부 작업 manifest에 넣을 수 있게 반환합니다."""
        return {"sources": [source.to_mapping() for source in self.sources]}


@dataclass(frozen=True, slots=True)
class GeometryInput:
    """원본 출처와 분리하여 유효면·참조 정점만 보관하는 공통 좌표 정렬 입력입니다."""

    owner_model_ids: tuple[str, ...]
    geometry_kind: str
    geometry_source: str
    positions_float64: np.ndarray
    triangles_uint32: np.ndarray | None
    source_fingerprint: str
    provenance: GeometryProvenance

    def __post_init__(self) -> None:
        """입력 배열을 외부 수정과 쓰기 플래그 변경으로부터 보호합니다."""
        object.__setattr__(self, "owner_model_ids", tuple(self.owner_model_ids))
        object.__setattr__(self, "positions_float64", _immutable_array(self.positions_float64, "<f8"))
        if self.triangles_uint32 is not None:
            object.__setattr__(self, "triangles_uint32", _immutable_array(self.triangles_uint32, "<u4"))


@dataclass(frozen=True, slots=True)
class CcCoreGeometryProvenance:
    """선택 P·A 전체와 초기 정렬·마커 서명을 순수 값으로 보관합니다."""

    moving_model_id: str
    reference_model_ids: tuple[str, ...]
    initial_alignment_float64: tuple[tuple[float, ...], ...]
    moving: GeometrySourceProvenance
    references: tuple[GeometrySourceProvenance, ...]
    marker_signature: tuple[tuple, ...]

    def to_mapping(self) -> dict[str, Any]:
        """저장·외부 계산·입력 지문에 공통으로 사용할 작은 값 사전을 만듭니다."""
        return {
            "schema": _SCHEMA,
            "moving_model_id": self.moving_model_id,
            "reference_model_ids": self.reference_model_ids,
            "initial_alignment_float64": self.initial_alignment_float64,
            "moving": self.moving.to_mapping(),
            "references": [source.to_mapping() for source in self.references],
            "marker_signature": self.marker_signature,
        }

    def without_content_hashes(self) -> CcCoreGeometryProvenance:
        """원본 내용 해시를 제외한 카탈로그·변환·마커 상태를 반환합니다."""
        return replace(self, moving=self.moving.without_content_hashes(),
                       references=tuple(source.without_content_hashes() for source in self.references))


@dataclass(frozen=True, slots=True)
class CcCoreGeometrySnapshotMetadata:
    """파일 전송 뒤 대형 좌표를 해제해도 재검증할 수 있는 작은 스냅샷입니다."""

    provenance: CcCoreGeometryProvenance
    input_fingerprint: str
    geometry_mode: str
    reverse_registration: bool

    @property
    def geometry_dependencies(self) -> tuple[tuple[str, str], ...]:
        """승인 출처의 축 변경을 추적할 소유자와 실제 형상 슬롯을 반환합니다."""
        sources = (self.provenance.moving, *self.provenance.references)
        return tuple(sorted({(source.owner_model_id, source.geometry_source) for source in sources}))

    @property
    def moving_model_id(self) -> str:
        """결과 적용 대상인 원래 P 소유자를 반환합니다."""
        return self.provenance.moving_model_id

    @property
    def reference_model_ids(self) -> tuple[str, ...]:
        """계산에 사용한 A 전체의 순서를 반환합니다."""
        return self.provenance.reference_model_ids


@dataclass(frozen=True, slots=True)
class CcCoreGeometrySnapshot:
    """외부 정렬에 전달할 불변 P·A 형상과 입력 감사 기록을 보관합니다."""

    moving: GeometryInput
    references: tuple[GeometryInput, ...]
    reference_combined: GeometryInput
    input_fingerprint: str
    geometry_mode: str
    reverse_registration: bool
    provenance: CcCoreGeometryProvenance

    @property
    def geometry_dependencies(self) -> tuple[tuple[str, str], ...]:
        """입력에 사용한 실제 형상별 축 종속성을 작은 감사 기록으로 반환합니다."""
        return self.without_arrays().geometry_dependencies

    def without_arrays(self) -> CcCoreGeometrySnapshotMetadata:
        """원본 지문 재검증에 필요한 값만 남겨 변환 좌표의 보관을 끝냅니다."""
        return CcCoreGeometrySnapshotMetadata(self.provenance, self.input_fingerprint,
                                            self.geometry_mode, self.reverse_registration)


@dataclass(frozen=True, slots=True)
class _SelectedGeometry:
    """카탈로그에서 선택한 형상과 소유 레이어를 임시로 연결합니다."""

    record: Any
    geometry_kind: str
    geometry_source: str
    model: Any


def _kind_label(kind: str) -> str:
    """오류와 모드 요약에 사용할 한국어 형상 이름을 반환합니다."""
    return "점군" if kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD else "메시"


def _selected_geometry(repository: Any, record: Any) -> _SelectedGeometry:
    """숨김 여부와 무관하게 레이어의 마지막 단일 형상 선택을 해석합니다."""
    name = str(record.display_name)
    points = bool(record.show_points)
    surface = bool(record.show_surface)
    if points == surface:
        raise CcCoreGeometryError(f"{name}: 점군 또는 메시 하나를 선택하십시오.")
    owner_kind = str(record.geometry_kind)
    if owner_kind not in {GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH}:
        raise CcCoreGeometryError(f"{name}: 지원하지 않는 레이어 형상 종류입니다.")
    if points and owner_kind != GEOMETRY_KIND_COLMAP_POINT_CLOUD:
        raise CcCoreGeometryError(f"{name}: 독립 메시를 점군으로 사용할 수 없습니다.")
    source = (MARKER_GEOMETRY_SOURCE_COMPANION
              if surface and owner_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD
              else MARKER_GEOMETRY_SOURCE_MAIN)
    kind = GEOMETRY_KIND_COLMAP_POINT_CLOUD if points else GEOMETRY_KIND_TRIANGLE_MESH
    model = marker_geometry_model_for_source(repository, str(record.model_id), source)
    if model is None:
        label = "연결 메시" if source == MARKER_GEOMETRY_SOURCE_COMPANION else _kind_label(kind)
        raise CcCoreGeometryError(f"{name}: 선택한 {label} 원본이 없습니다.")
    if geometry_kind_for_model(model) != kind:
        raise CcCoreGeometryError(f"{name}: 선택한 {_kind_label(kind)}와 실제 원본 형상 종류가 다릅니다.")
    return _SelectedGeometry(record, kind, source, model)


def _marker_selection_reason(runtime: Any, moving: _SelectedGeometry,
                             references: tuple[_SelectedGeometry, ...]) -> str | None:
    """역할별 마커 종류와 저장된 원본 슬롯이 현재 형상 선택과 같은지 검사합니다."""
    selection = getattr(runtime, "marker_selection", None)
    pairs = tuple(getattr(selection, "completed_pairs", ()))
    if not pairs:
        return None
    conflict = getattr(selection, "geometry_conflict_message", None)
    if callable(conflict):
        reason = conflict()
        if reason:
            return str(reason)
    by_id = {str(item.record.model_id): item for item in references}
    for role, label, selected in ((ROLE_REFERENCE, "A", references[0]), (ROLE_MOVING, "P", moving)):
        kind_getter = getattr(selection, "role_geometry_kind", None)
        locked = kind_getter(role) if callable(kind_getter) else None
        if locked is not None and locked != selected.geometry_kind:
            return (f"{label} 마커는 {_kind_label(locked)}로 고정되어 있습니다. "
                    f"{selected.record.display_name}의 선택 형상과 일치시킨 뒤 정렬하십시오.")
        for pair in pairs:
            point = pair.reference if role == ROLE_REFERENCE else pair.moving
            owner_id = str(point.model_id)
            expected = by_id.get(owner_id) if role == ROLE_REFERENCE else moving
            if expected is None or owner_id != str(expected.record.model_id):
                return f"{label} 마커가 현재 정렬 대상에 없는 레이어를 참조합니다: {owner_id}"
            point_kind = str(point.point_kind)
            if point_kind not in {MARKER_POINT_KIND_COLMAP, MARKER_POINT_KIND_MESH_VERTEX, MARKER_POINT_KIND_MESH_SURFACE}:
                return f"{label} 마커의 저장된 형상 종류가 올바르지 않습니다."
            marker_kind = (GEOMETRY_KIND_COLMAP_POINT_CLOUD if point_kind == MARKER_POINT_KIND_COLMAP
                           else GEOMETRY_KIND_TRIANGLE_MESH)
            source = str(getattr(point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN))
            if marker_kind != expected.geometry_kind or source != expected.geometry_source:
                return (f"{label} 마커가 참조한 {_kind_label(marker_kind)} 형상과 "
                        f"{expected.record.display_name}의 현재 선택 형상이 다릅니다. 같은 형상을 선택하십시오.")
    return None


def _selected_geometries(runtime: Any, moving_model_id: str | None) -> tuple[_SelectedGeometry, tuple[_SelectedGeometry, ...]]:
    """선택 P 하나와 숨김을 포함한 A 전체의 형상 및 마커 계약을 검증합니다."""
    catalog = runtime.model_catalog
    moving_ids = tuple(catalog.selected_moving_model_ids)
    if len(moving_ids) != 1:
        raise CcCoreGeometryError("정렬할 P 레이어를 정확히 하나 선택하십시오.")
    actual_moving_id = str(moving_ids[0])
    if moving_model_id is not None and str(moving_model_id) != actual_moving_id:
        raise CcCoreGeometryError("정렬할 P 레이어 선택이 계산 대상과 다릅니다.")
    reference_records = tuple(catalog.active_models())
    if not reference_records:
        raise CcCoreGeometryError("정렬 기준으로 사용할 A 레이어가 없습니다.")
    reference_ids = tuple(str(record.model_id) for record in reference_records)
    if actual_moving_id in reference_ids or len(set(reference_ids)) != len(reference_ids):
        raise CcCoreGeometryError("정렬 대상의 A/P 소속 또는 A 목록이 올바르지 않습니다.")
    moving_record = catalog.models.get(actual_moving_id)
    if moving_record is None:
        raise CcCoreGeometryError("선택한 P 레이어가 없습니다.")
    repository = runtime.model_repository
    references = tuple(_selected_geometry(repository, record) for record in reference_records)
    moving = _selected_geometry(repository, moving_record)
    if len({item.geometry_kind for item in references}) != 1:
        details = ", ".join(f"{item.record.display_name}({_kind_label(item.geometry_kind)})" for item in references)
        raise CcCoreGeometryError(f"A 레이어의 입력 형상이 혼합되어 있습니다: {details}. 모두 같은 종류를 선택하십시오.")
    marker_reason = _marker_selection_reason(runtime, moving, references)
    if marker_reason:
        raise CcCoreGeometryError(marker_reason)
    return moving, references


def geometry_selection_block_reason(runtime: Any, moving_model_id: str | None = None) -> str | None:
    """대형 좌표 검사나 해시 없이 카탈로그와 마커의 계산 차단 사유를 반환합니다."""
    try:
        _selected_geometries(runtime, moving_model_id)
    except (ValueError, RuntimeError, KeyError, AttributeError, TypeError) as error:
        return str(error) if isinstance(error, (ValueError, RuntimeError)) else "정렬할 레이어 또는 형상 정보를 읽을 수 없습니다."
    return None


def geometry_mode_summary(runtime: Any, moving_model_id: str | None = None) -> str:
    """현재 선택에서 P와 A 전체의 입력 형상을 짧은 한국어 문장으로 반환합니다."""
    try:
        moving, references = _selected_geometries(runtime, moving_model_id)
    except (ValueError, RuntimeError, KeyError, AttributeError, TypeError):
        return "입력 형상 선택 확인 필요"
    return f"P {_kind_label(moving.geometry_kind)} → A {_kind_label(references[0].geometry_kind)} 전체 {len(references)}개"


def _raw_arrays(selected: _SelectedGeometry) -> tuple[np.ndarray, np.ndarray | None]:
    """희소 등록점 또는 메시 전체 정점·삼각면을 원본 플래그 변경 없이 읽습니다."""
    model = selected.model
    raw = (getattr(getattr(model, "points", None), "positions", None)
           if selected.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD
           else getattr(model, "vertices_float64", None))
    name = str(selected.record.display_name)
    if raw is None:
        raise CcCoreGeometryError(f"{name}: 선택 형상의 원본 좌표 배열이 없습니다.")
    positions = np.asarray(raw)
    if positions.ndim != 2 or positions.shape[1] != 3 or positions.shape[0] < 3:
        raise CcCoreGeometryError(f"{name}: 정렬 좌표는 점이 3개 이상인 N×3 배열이어야 합니다.")
    if positions.dtype.kind not in "fiu":
        raise CcCoreGeometryError(f"{name}: 원본 좌표가 실수 배열이 아닙니다.")
    if selected.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD:
        return positions, None
    raw_triangles = getattr(model, "triangles_uint32", None)
    if raw_triangles is None:
        raise CcCoreGeometryError(f"{name}: 메시의 삼각면 배열이 없습니다.")
    triangles = np.asarray(raw_triangles)
    if triangles.ndim != 2 or triangles.shape[1] != 3 or not triangles.shape[0]:
        raise CcCoreGeometryError(f"{name}: 메시의 삼각면 배열이 비었거나 형식이 잘못되었습니다.")
    if triangles.dtype.kind not in "iu":
        raise CcCoreGeometryError(f"{name}: 삼각면 인덱스는 정수여야 합니다.")
    if positions.shape[0] > np.iinfo(np.uint32).max:
        raise CcCoreGeometryError(f"{name}: 메시 정점 수가 삼각면 인덱스의 지원 범위를 넘습니다.")
    return positions, triangles


def _validate_raw_geometry(
    positions: np.ndarray, triangles: np.ndarray | None, name: str,
) -> tuple[np.ndarray, np.ndarray | None]:
    """손상 입력은 거부하고 메시의 퇴화면·미참조 정점만 정렬용 사본에서 제외합니다."""
    if triangles is not None:
        try:
            return prepare_mesh_alignment_geometry(positions, triangles)
        except ValueError as error:
            raise CcCoreGeometryError(f"{name}: {error}") from error
    for start in range(0, len(positions), _BLOCK_ROWS):
        if not np.isfinite(positions[start:start + _BLOCK_ROWS]).all():
            raise CcCoreGeometryError(f"{name}: 원본 좌표에 유한하지 않은 값이 있습니다.")
    if triangles is None:
        minimum = positions.min(axis=0).astype(np.float64)
        maximum = positions.max(axis=0).astype(np.float64)
        with np.errstate(over="ignore", invalid="ignore"):
            span = maximum - minimum
        if not np.isfinite(span).all() or float(span.max()) <= 0.0:
            raise CcCoreGeometryError(f"{name}: 점군 범위가 퇴화되었거나 수치 범위를 벗어났습니다.")
        axis = int(span.argmax())
        origin = positions[int(positions[:, axis].argmin())].astype(np.float64)
        direction = (positions[int(positions[:, axis].argmax())] - origin) / span.max()
        for start in range(0, len(positions), _BLOCK_ROWS):
            offsets = (positions[start:start + _BLOCK_ROWS].astype(np.float64, copy=False) - origin) / span.max()
            if np.any(np.max(np.abs(np.cross(offsets, direction)), axis=1) > 32.0 * np.finfo(np.float64).eps):
                return positions, None
        raise CcCoreGeometryError(f"{name}: 모든 등록점이 한 직선에 놓인 퇴화 점군입니다.")


def _array_fingerprint(array: np.ndarray, dtype: str) -> str:
    """대형 배열 전체 복사 없이 표준 형식의 블록을 순서대로 해시합니다."""
    digest = hashlib.sha256()
    digest.update(f"{dtype}:{array.shape}".encode("ascii"))
    for start in range(0, len(array), _BLOCK_ROWS):
        block = np.ascontiguousarray(array[start:start + _BLOCK_ROWS], dtype=np.dtype(dtype))
        digest.update(memoryview(block).cast("B"))
    return digest.hexdigest()


def _marker_signature(runtime: Any) -> tuple[tuple, ...]:
    """마커의 역할·소유자·원본 슬롯·요소 ID·좌표를 불변 서명으로 보관합니다."""
    signature = []
    for pair in getattr(getattr(runtime, "marker_selection", None), "completed_pairs", ()):
        points = []
        for point in (pair.reference, pair.moving):
            position = tuple(float(component) for component in point.local_position)
            if len(position) != 3 or not all(np.isfinite(position)):
                raise CcCoreGeometryError("마커 원본 좌표에 유한하지 않은 값이 있습니다.")
            points.append((str(point.model_role), str(point.model_id), str(point.point_kind),
                           str(getattr(point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN)),
                           int(point.source_point_id), getattr(point, "point3d_id", None), position))
        signature.append((int(pair.pair_id), *points))
    return tuple(signature)


def _prepare_source(repository: Any, selected: _SelectedGeometry, applied_alignment: object | None,
                    *, verify_content: bool, validate_geometry: bool) -> tuple[GeometrySourceProvenance, np.ndarray, np.ndarray | None]:
    """원본 전체의 지문·개수를 보존하고 정렬에 전달할 배열만 별도로 정제합니다."""
    owner_id = str(selected.record.model_id)
    positions, triangles = _raw_arrays(selected)
    alignment_positions, alignment_triangles = positions, triangles
    if validate_geometry:
        alignment_positions, alignment_triangles = _validate_raw_geometry(
            positions, triangles, str(selected.record.display_name),
        )
    companion = selected.geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION
    getter = (repository.companion_display_transform_for_model_id if companion
              else repository.display_transform_for_model_id)
    display = getter(owner_id)
    committed = repository.alignment_transform_for_model_id(owner_id)
    display_matrix = _matrix_tuple(display.matrix_float64)
    committed_matrix = _matrix_tuple(committed.matrix_float64)
    applied = committed_matrix if applied_alignment is None else _matrix_tuple(applied_alignment)
    source = getattr(repository, "companion_sources" if companion else "source_metadata", {}).get(owner_id)
    provenance = GeometrySourceProvenance(
        owner_model_id=owner_id, geometry_kind=selected.geometry_kind,
        geometry_source=selected.geometry_source, geometry_revision=int(getattr(selected.model, "geometry_revision", 0)),
        source_path=str(getattr(selected.model, "source_path", "")),
        source_fingerprint=str(getattr(source, "source_fingerprint", "")),
        positions_fingerprint=_array_fingerprint(positions, "<f8") if verify_content else "",
        triangles_fingerprint=_array_fingerprint(triangles, "<u4") if verify_content and triangles is not None else "",
        point_count=len(positions), triangle_count=0 if triangles is None else len(triangles),
        display_transform_fingerprint=str(display.fingerprint), display_transform_float64=display_matrix,
        committed_alignment_fingerprint=str(committed.fingerprint), committed_alignment_float64=committed_matrix,
        applied_alignment_float64=applied,
    )
    return provenance, alignment_positions, alignment_triangles


def _geometry_input(prepared: tuple[GeometrySourceProvenance, np.ndarray, np.ndarray | None]) -> GeometryInput:
    """원본 좌표에 공통 정렬 C와 선택 형상 축 N을 순서대로 적용합니다."""
    source, positions, triangles = prepared
    combined = np.asarray(source.applied_alignment_float64) @ np.asarray(source.display_transform_float64)
    with np.errstate(over="ignore", invalid="ignore"):
        transformed = np.asarray(positions, dtype=np.float64) @ combined[:3, :3].T
        transformed += combined[:3, 3]
    if not np.isfinite(transformed).all():
        raise CcCoreGeometryError(f"{source.owner_model_id}: 공통 좌표 변환 결과가 유한하지 않습니다.")
    provenance = GeometryProvenance((source,))
    return GeometryInput((source.owner_model_id,), source.geometry_kind, source.geometry_source,
                         transformed, triangles, _digest_value(provenance.to_mapping()), provenance)


def _combine_references(references: tuple[GeometryInput, ...]) -> GeometryInput:
    """각 A의 변환 완료 좌표를 합치고 삼각면 정점 인덱스에 오프셋을 반영합니다."""
    if len(references) == 1:
        return references[0]
    count = sum(len(item.positions_float64) for item in references)
    is_mesh = references[0].geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH
    if is_mesh and count > np.iinfo(np.uint32).max:
        raise CcCoreGeometryError("A 메시 전체 정점 수가 삼각면 인덱스의 지원 범위를 넘습니다.")
    positions = np.empty((count, 3), dtype=np.float64)
    face_count = sum(len(item.triangles_uint32) for item in references) if is_mesh else 0
    triangles = np.empty((face_count, 3), dtype=np.uint32) if is_mesh else None
    offset = face_offset = 0
    for item in references:
        item_count = len(item.positions_float64)
        positions[offset:offset + item_count] = item.positions_float64
        if triangles is not None:
            item_faces = len(item.triangles_uint32)
            np.add(item.triangles_uint32, np.uint32(offset), out=triangles[face_offset:face_offset + item_faces])
            face_offset += item_faces
        offset += item_count
    sources = tuple(source for item in references for source in item.provenance.sources)
    provenance = GeometryProvenance(sources)
    slots = {item.geometry_source for item in references}
    return GeometryInput(tuple(source.owner_model_id for source in sources), references[0].geometry_kind,
                         next(iter(slots)) if len(slots) == 1 else "MIXED_SOURCE", positions, triangles,
                         _digest_value(provenance.to_mapping()), provenance)


def _capture_sources(runtime: Any, *, moving_model_id: str, reference_model_ids: tuple[str, ...],
                     initial_alignment_float64: object, verify_content: bool, validate_geometry: bool):
    """A 전체 목록을 확인하고 변환 좌표를 만들기 전 원본 지문 자료를 캡처합니다."""
    moving, references = _selected_geometries(runtime, moving_model_id)
    actual_ids = tuple(str(item.record.model_id) for item in references)
    if tuple(reference_model_ids) != actual_ids:
        raise CcCoreGeometryError("정렬 기준 A 목록이 현재 A 그룹 전체와 다릅니다. 입력을 다시 준비하십시오.")
    initial = _matrix_tuple(initial_alignment_float64)
    repository = runtime.model_repository
    moving_source = _prepare_source(repository, moving, initial,
                                    verify_content=verify_content, validate_geometry=validate_geometry)
    reference_sources = tuple(_prepare_source(repository, item, None,
                              verify_content=verify_content, validate_geometry=validate_geometry) for item in references)
    provenance = CcCoreGeometryProvenance(str(moving.record.model_id), actual_ids, initial,
                                        moving_source[0], tuple(item[0] for item in reference_sources),
                                        _marker_signature(runtime))
    return provenance, moving_source, reference_sources


def create_cccore_geometry_snapshot(runtime: Any, *, moving_model_id: str,
                                    reference_model_ids: tuple[str, ...],
                                    initial_alignment_float64: object) -> CcCoreGeometrySnapshot:
    """선택 형상을 한 번 검증·해시한 뒤 초기 P와 확정 A를 불변 공통 좌표로 만듭니다."""
    provenance, moving_source, reference_sources = _capture_sources(
        runtime, moving_model_id=moving_model_id, reference_model_ids=tuple(reference_model_ids),
        initial_alignment_float64=initial_alignment_float64, verify_content=True, validate_geometry=True)
    moving = _geometry_input(moving_source)
    references = tuple(_geometry_input(item) for item in reference_sources)
    kinds = ("POINT" if item.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD else "MESH"
             for item in (moving, references[0]))
    mode = "_TO_".join(kinds)
    return CcCoreGeometrySnapshot(moving, references, _combine_references(references),
                                 _digest_value(provenance.to_mapping()), mode, mode == "MESH_TO_POINT", provenance)


def geometry_snapshot_validation_message(runtime: Any,
                                         snapshot: CcCoreGeometrySnapshot | CcCoreGeometrySnapshotMetadata,
                                         *, initial_alignment_float64: object | None = None,
                                         verify_content: bool = True) -> str | None:
    """변환 배열을 재생성하지 않고 현재 원본·축·정렬·선택·마커가 같은지 확인합니다."""
    expected = snapshot.provenance
    initial = expected.initial_alignment_float64 if initial_alignment_float64 is None else initial_alignment_float64
    try:
        current, _moving, _references = _capture_sources(
            runtime, moving_model_id=expected.moving_model_id, reference_model_ids=expected.reference_model_ids,
            initial_alignment_float64=initial, verify_content=verify_content, validate_geometry=False)
    except (ValueError, RuntimeError, KeyError, AttributeError, TypeError, OverflowError) as error:
        return f"정렬 입력 형상 또는 기준이 바뀌었습니다. 다시 계산하십시오. {error}"
    if not verify_content:
        expected = expected.without_content_hashes()
    if current != expected:
        return "정렬 입력의 원본 형상·축·정렬·A/P 선택 또는 마커가 바뀌었습니다. 다시 계산하십시오."
    return None
