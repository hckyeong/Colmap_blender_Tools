"""Blender 의존성 없이 프로젝트 저장 스키마 값을 보관합니다."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..alignment.types import AlignmentProvenanceRecord
from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    MARKER_POINT_KIND_COLMAP,
)


@dataclass(frozen=True, slots=True)
class ProjectSourceSnapshot:
    """한 모델을 다시 읽는 데 필요한 작은 원본 메타데이터를 보관합니다."""

    selected_root_absolute: str
    sparse_path_absolute: str
    sparse_path_relative: str
    source_format: str
    source_fingerprint: str
    display_point_limit: int
    axis_preset: str
    axis_forward: str
    axis_up: str
    display_transform_fingerprint: str
    geometry_kind: str = GEOMETRY_KIND_COLMAP_POINT_CLOUD
    kind: str = ""
    path_absolute: str = ""
    path_relative: str = ""
    display_vertex_limit: int = 0

    def __post_init__(self) -> None:
        """v1 COLMAP 필드를 유지하면서 v2 source 종류·경로 별칭을 정규화합니다."""
        geometry_kind = str(self.geometry_kind).strip().upper() or GEOMETRY_KIND_COLMAP_POINT_CLOUD
        kind = str(self.kind).strip().upper() or ("OBJ" if geometry_kind == "TRIANGLE_MESH" else "COLMAP")
        sparse_path_absolute = str(self.sparse_path_absolute).strip()
        sparse_path_relative = str(self.sparse_path_relative).strip()
        path_absolute = str(self.path_absolute).strip() or sparse_path_absolute
        path_relative = str(self.path_relative).strip() or sparse_path_relative
        selected_root = str(self.selected_root_absolute).strip() or path_absolute
        display_vertex_limit = int(self.display_vertex_limit or self.display_point_limit)
        object.__setattr__(self, "geometry_kind", geometry_kind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "selected_root_absolute", selected_root)
        object.__setattr__(self, "sparse_path_absolute", sparse_path_absolute)
        object.__setattr__(self, "sparse_path_relative", sparse_path_relative)
        object.__setattr__(self, "path_absolute", path_absolute)
        object.__setattr__(self, "path_relative", path_relative)
        object.__setattr__(self, "display_vertex_limit", display_vertex_limit)


@dataclass(frozen=True, slots=True)
class ProjectCatalogSnapshot:
    """Data Layers의 확정 그룹·표시·감사 상태를 보관합니다."""

    registration_status: str
    is_anchor: bool
    is_visible: bool
    alignment_provenance: AlignmentProvenanceRecord | None


@dataclass(frozen=True, slots=True)
class ProjectModelSnapshot:
    """한 런타임 모델의 source·카탈로그·확정 Sim(3) 상태를 묶습니다."""

    model_id: str
    display_name: str
    load_order: int
    source: ProjectSourceSnapshot
    catalog: ProjectCatalogSnapshot
    alignment_matrix_float64: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True, slots=True)
class ProjectMarkerPointSnapshot:
    """완료 마커 한쪽의 모델·원본 요소 ID와 선택 좌표를 보관합니다."""

    model_role: str
    model_id: str
    point3d_id: int = 0
    point_kind: str = MARKER_POINT_KIND_COLMAP
    source_point_id: int | None = None
    local_position: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        """v1/v2 점 ID와 v3 면 위치를 호환 가능한 마커 참조로 정규화합니다."""
        source_id = self.point3d_id if self.source_point_id is None else self.source_point_id
        local_position = self.local_position
        if local_position is not None:
            local_position = tuple(float(value) for value in local_position)
            if len(local_position) != 3 or not all(math.isfinite(value) for value in local_position):
                raise ValueError("마커 Source Local 좌표가 올바르지 않습니다.")
        object.__setattr__(self, "source_point_id", int(source_id))
        object.__setattr__(self, "point3d_id", int(source_id))
        object.__setattr__(self, "point_kind", str(self.point_kind).strip().upper() or MARKER_POINT_KIND_COLMAP)
        object.__setattr__(self, "local_position", local_position)


@dataclass(frozen=True, slots=True)
class ProjectMarkerPairSnapshot:
    """한 완료 A/P 마커 쌍의 안정 식별자와 양쪽 원본 참조를 보관합니다."""

    pair_id: int
    reference: ProjectMarkerPointSnapshot
    moving: ProjectMarkerPointSnapshot


@dataclass(frozen=True, slots=True)
class ProjectMarkerSnapshot:
    """드래프트를 제외한 완료 마커 목록과 UI 선택 상태를 보관합니다."""

    next_pair_id: int
    active_pair_id: int | None
    completed_pairs: tuple[ProjectMarkerPairSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ProjectStateSnapshot:
    """`.blend` Text에 기록할 버전화된 전체 확정 작업 상태입니다."""

    schema_version: int
    writer_addon_version: tuple[int, int, int]
    project_uuid: str
    saved_at_utc: str
    models: tuple[ProjectModelSnapshot, ...]
    markers: ProjectMarkerSnapshot
    payload_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class ProjectRestoreIssue:
    """복원 UI가 원인·영향·해결을 함께 표시할 작은 오류 정보입니다."""

    cause: str
    impact: str
    solution: str
    model_id: str = ""
