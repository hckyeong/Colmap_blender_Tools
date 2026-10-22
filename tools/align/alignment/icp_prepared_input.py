"""불러오기 완료 전에 원본 지문과 정렬용 유효 형상을 준비해 모델 수명 동안 보관합니다."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..geometry_buffers import immutable_geometry_array, immutable_geometry_fingerprint, is_immutable_geometry


def seal_point_model(model: Any) -> Any:
    """COLMAP 원본 좌표만 불변 공유 배열로 고정하고 나머지 점·카메라 정보는 유지합니다."""
    if hasattr(model, 'vertices_float64') or is_immutable_geometry(model.points.positions):
        return model
    return replace(model, points=replace(model.points,
        positions=immutable_geometry_array(model.points.positions, '<f8')))


@dataclass(frozen=True)
class PreparedAlignmentInput:
    """원본 객체·개정·배열과 계산용 유효 형상 및 검사 실패 원인을 묶습니다."""
    model: Any
    revision: int
    raw_positions: Any
    raw_triangles: Any
    positions_fingerprint: str
    triangles_fingerprint: str
    positions: Any
    triangles: Any
    error: str = ''

    def matches(self, model: Any) -> bool:
        """원본 내용 읽기 없이 모델·개정·불변 배열 교체 여부를 확인합니다."""
        positions, triangles = model_arrays(model)
        return (model is self.model and int(model.geometry_revision) == self.revision
            and positions is self.raw_positions and triangles is self.raw_triangles
            and is_immutable_geometry(positions)
            and (triangles is None or is_immutable_geometry(triangles)))


def model_arrays(model: Any) -> tuple:
    """원본 메시 또는 COLMAP 점군의 좌표·면 배열을 참조로 반환합니다."""
    if hasattr(model, 'vertices_float64'):
        return model.vertices_float64, model.triangles_uint32
    return model.points.positions, None


def prepare_alignment_input(model: Any) -> PreparedAlignmentInput:
    """최초 전체 검사 결과를 보관하고 퇴화 부분만 정렬용 배열에서 분리합니다."""
    from .icp_geometry import _validate_raw_geometry, CcCoreGeometryError
    positions, triangles = model_arrays(model)
    position_hash = immutable_geometry_fingerprint(positions, '<f8')
    triangle_hash = '' if triangles is None else immutable_geometry_fingerprint(triangles, '<u4')
    error = ''
    prepared_positions, prepared_triangles = positions, triangles
    try:
        prepared_positions, prepared_triangles = _validate_raw_geometry(positions, triangles, model.display_name)
        prepared_positions = immutable_geometry_array(prepared_positions, '<f8')
        if prepared_triangles is not None:
            prepared_triangles = immutable_geometry_array(prepared_triangles, '<u4')
    except CcCoreGeometryError as failure:
        error = str(failure)
    return PreparedAlignmentInput(model, int(model.geometry_revision), positions, triangles,
        position_hash, triangle_hash, prepared_positions, prepared_triangles, error)


def registered_alignment_input(repository: Any, model_id: str, geometry_source: str) -> PreparedAlignmentInput:
    """저장소 슬롯의 준비된 불변 입력만 반환하고 교체·누락·검사 실패를 거부합니다."""
    from .icp_geometry import CcCoreGeometryError
    model = (repository.companion_meshes if geometry_source == 'COMPANION' else repository.models).get(model_id)
    entry = repository.alignment_inputs.get((model_id, geometry_source))
    if entry is None or model is None or not entry.matches(model):
        raise CcCoreGeometryError('정렬 원본이 준비 이후 교체되었습니다. 형상을 다시 불러오십시오.')
    if entry.error:
        raise CcCoreGeometryError(entry.error)
    return entry
