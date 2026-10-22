"""실제 COLMAP 모델 배열과 경량 UI 카탈로그를 분리해 보관합니다."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import numpy as np

from .alignment.types import Sim3Transform, identity_sim3_transform
from .colmap_io.types import ColmapModelData
from .constants import GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH
from .display_axes import ModelDisplayTransform
from .logging_utils import get_logger
from .mesh_io.types import TriangleMeshModelData


_LOGGER = get_logger("model_repository")


class ModelRepositoryError(RuntimeError):
    """모델 저장소 등록 경계에서 사용자에게 전달할 오류입니다."""


class DuplicateModelSourceError(ModelRepositoryError):
    """기존 호출부의 예외 참조 호환을 위해 유지하는 원본 경로 중복 오류입니다."""


@dataclass(frozen=True, slots=True)
class ImportedModelSourceMetadata:
    """저장 시 재해시 없이 사용할 검증 완료 COLMAP·OBJ 원본 메타데이터입니다."""

    selected_root_absolute: str
    sparse_path_absolute: str
    source_format: str
    source_fingerprint: str
    display_point_limit: int
    axis_preset: str
    axis_forward: str
    axis_up: str
    display_transform_fingerprint: str
    geometry_kind: str = GEOMETRY_KIND_COLMAP_POINT_CLOUD

    def __post_init__(self) -> None:
        """경량 저장 메타데이터의 경로·지문·표시 설정을 정규화합니다."""
        selected_root = str(self.selected_root_absolute).strip()
        sparse_path = str(self.sparse_path_absolute).strip()
        source_format = str(self.source_format).strip().lower()
        fingerprint = str(self.source_fingerprint).strip().lower()
        display_limit = int(self.display_point_limit)
        geometry_kind = str(self.geometry_kind).strip().upper()
        if not selected_root or not sparse_path:
            raise ModelRepositoryError("저장할 원본 경로가 비어 있습니다.")
        if geometry_kind not in {GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH}:
            raise ModelRepositoryError("저장할 geometry kind가 올바르지 않습니다.")
        if geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD and source_format not in {"binary", "text"}:
            raise ModelRepositoryError("저장할 COLMAP 원본 형식이 올바르지 않습니다.")
        if geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH and source_format != "obj":
            raise ModelRepositoryError("삼각형 메시 원본 형식은 obj여야 합니다.")
        fingerprint_required = geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD
        if (fingerprint_required and len(fingerprint) != 64) or (
            fingerprint and (len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint))
        ):
            raise ModelRepositoryError("저장할 COLMAP source fingerprint가 SHA-256 형식이 아닙니다.")
        if display_limit < 1:
            raise ModelRepositoryError("저장할 COLMAP 표시 점 상한은 1 이상이어야 합니다.")
        object.__setattr__(self, "selected_root_absolute", canonical_source_path(selected_root))
        object.__setattr__(self, "sparse_path_absolute", canonical_source_path(sparse_path))
        object.__setattr__(self, "source_format", source_format)
        object.__setattr__(self, "source_fingerprint", fingerprint)
        object.__setattr__(self, "display_point_limit", display_limit)
        object.__setattr__(self, "axis_preset", str(self.axis_preset).strip())
        object.__setattr__(self, "axis_forward", str(self.axis_forward).strip())
        object.__setattr__(self, "axis_up", str(self.axis_up).strip())
        object.__setattr__(self, "geometry_kind", geometry_kind)
        object.__setattr__(
            self,
            "display_transform_fingerprint",
            str(self.display_transform_fingerprint).strip(),
        )

    @property
    def source_path_absolute(self) -> str:
        """COLMAP sparse 경로 또는 OBJ 파일 경로를 공통 source 경로로 반환합니다."""

        return self.sparse_path_absolute


@dataclass(frozen=True, slots=True)
class CompanionMeshSourceMetadata:
    """COLMAP 소유 메시를 형상 전용으로 다시 읽는 데 필요한 출처입니다."""

    source_path_absolute: str
    source_fingerprint: str
    geometry_only: bool = True

    def __post_init__(self) -> None:
        """연결 OBJ 경로·SHA-256·형상 전용 정책을 검증합니다."""
        source_path = str(self.source_path_absolute).strip()
        fingerprint = str(self.source_fingerprint).strip().lower()
        if not source_path or Path(source_path).suffix.casefold() != ".obj":
            raise ModelRepositoryError("연결 메시 출처는 OBJ 파일 경로여야 합니다.")
        if fingerprint and (len(fingerprint) != 64 or any(value not in "0123456789abcdef" for value in fingerprint)):
            raise ModelRepositoryError("연결 메시 출처 지문은 SHA-256이어야 합니다.")
        if self.geometry_only is not True:
            raise ModelRepositoryError("연결 메시에는 형상 전용 읽기 정책이 필요합니다.")
        object.__setattr__(self, "source_path_absolute", canonical_source_path(source_path))
        object.__setattr__(self, "source_fingerprint", fingerprint)


def canonical_source_path(source_path: str | os.PathLike[str]) -> str:
    """COLMAP·OBJ 원본 경로를 출처 조회에 사용할 플랫폼 규칙으로 정규화합니다."""
    normalized = Path(source_path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(normalized)))


def create_model_id() -> str:
    """런타임 모델을 구분할 충돌 가능성이 낮은 UUID 문자열을 생성합니다."""
    return uuid4().hex


def _read_parsed_field(parsed_model: Any, field_name: str, default: Any = None) -> Any:
    """I/O 타입의 속성 또는 매핑 필드를 느슨하게 읽어 호환 경계를 유지합니다."""
    if isinstance(parsed_model, dict):
        return parsed_model.get(field_name, default)
    return getattr(parsed_model, field_name, default)


def _point_positions(points: Any) -> np.ndarray:
    """원본 points 테이블에서 유한한 float64 XYZ 배열을 검증해 반환합니다."""
    positions = _read_parsed_field(points, "positions")
    if positions is None:
        raise ModelRepositoryError("points3D 좌표 배열을 찾을 수 없습니다.")
    array = np.asarray(positions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 3 or array.shape[0] == 0:
        raise ModelRepositoryError("points3D 좌표 배열은 비어 있지 않은 N×3 배열이어야 합니다.")
    if not np.isfinite(array).all():
        raise ModelRepositoryError("points3D 좌표 배열에 유한하지 않은 값이 있습니다.")
    return array


def build_model_data(
    parsed_model: Any,
    source_path: str | os.PathLike[str],
    *,
    model_id_factory: Callable[[], str] = create_model_id,
    display_name: str | None = None,
) -> ColmapModelData | TriangleMeshModelData:
    """COLMAP·메시의 불변 원본에 런타임 ID·표시 이름·정규 경로를 부여합니다."""
    canonical_path = canonical_source_path(source_path)
    resolved_name = (display_name or Path(canonical_path).stem or Path(canonical_path).name).strip()
    if not resolved_name:
        raise ModelRepositoryError("모델 표시 이름을 결정할 수 없습니다.")
    model_id = str(model_id_factory()).strip()
    if not model_id:
        raise ModelRepositoryError("모델 식별자를 생성할 수 없습니다.")
    if isinstance(parsed_model, TriangleMeshModelData):
        return replace(
            parsed_model,
            model_id=model_id,
            display_name=resolved_name,
            source_path=canonical_path,
            geometry_revision=max(1, int(parsed_model.geometry_revision)),
        )
    points = _read_parsed_field(parsed_model, "points")
    if points is None:
        raise ModelRepositoryError("가져온 COLMAP 모델에 points3D 데이터가 없습니다.")
    _point_positions(points)
    if not isinstance(parsed_model, ColmapModelData):
        raise ModelRepositoryError("COLMAP I/O가 반환한 모델 형식이 올바르지 않습니다.")
    from .geometry_buffers import immutable_geometry_array
    return replace(
        parsed_model,
        points=replace(points, positions=immutable_geometry_array(points.positions, '<f8')),
        model_id=model_id,
        display_name=resolved_name,
        source_path=canonical_path,
        geometry_revision=max(1, int(parsed_model.geometry_revision)),
    )


def model_point_count(model: ColmapModelData | TriangleMeshModelData) -> int:
    """COLMAP 등록점 또는 메시 등록 정점 개수를 로그·UI 요약용으로 반환합니다."""
    if isinstance(model, TriangleMeshModelData):
        return int(model.registration_vertex_indices.shape[0])
    point_ids = _read_parsed_field(model.points, "point_ids")
    return len(point_ids) if point_ids is not None else 0


def model_camera_count(model: ColmapModelData | TriangleMeshModelData) -> int:
    """I/O 카메라 테이블에서 로그·UI 요약에 사용할 카메라 개수를 반환합니다."""
    return len(getattr(model, "cameras", ())) if getattr(model, "cameras", None) is not None else 0


def model_image_count(model: ColmapModelData | TriangleMeshModelData) -> int:
    """I/O 이미지 테이블에서 로그·UI 요약에 사용할 이미지 개수를 반환합니다."""
    images = getattr(model, "images", None)
    if images is None:
        return 0
    count = _read_parsed_field(images, "count")
    if count is not None:
        return int(count)
    records = _read_parsed_field(images, "image_records", images)
    return len(records) if records is not None else 0


@dataclass
class ColmapModelRepository:
    """독립 모델과 경로별 첫 생존 모델을 가리키는 출처 색인을 소유합니다."""

    models: dict[str, ColmapModelData | TriangleMeshModelData]
    source_path_index: dict[str, str]
    display_transforms: dict[str, ModelDisplayTransform]
    alignment_transforms: dict[str, Sim3Transform]
    source_metadata: dict[str, ImportedModelSourceMetadata]
    icp_confidence_cache: dict[str, tuple[int, Any]]
    companion_meshes: dict[str, TriangleMeshModelData]
    companion_sources: dict[str, CompanionMeshSourceMetadata]
    companion_display_transforms: dict[str, ModelDisplayTransform]

    def __init__(self) -> None:
        """원본 모델·연결 메시와 모델 수명의 정렬 입력을 초기화합니다."""
        self.models = {}
        self.source_path_index = {}
        self.display_transforms = {}
        self.alignment_transforms = {}
        self.source_metadata = {}
        self.icp_confidence_cache = {}
        self.companion_meshes = {}
        self.companion_sources = {}
        self.companion_display_transforms = {}
        self.alignment_inputs = {}

    def alignment_input_for(self, model_id: str, geometry_source: str):
        """등록 때 검증한 동일 형상만 반환하고 미등록·교체·검사 실패를 거부합니다."""
        from .alignment.icp_prepared_input import registered_alignment_input
        return registered_alignment_input(self, model_id, geometry_source)

    def cached_icp_confidence_for_model_id(self, model_id: str) -> Any | None:
        """현재 geometry revision과 일치하는 ICP 점 분포 신뢰도만 반환합니다."""

        model = self.models.get(str(model_id))
        cached = self.icp_confidence_cache.get(str(model_id))
        if model is None or cached is None:
            return None
        revision, confidence = cached
        if int(revision) != int(getattr(model, "geometry_revision", -1)):
            self.icp_confidence_cache.pop(str(model_id), None)
            return None
        return confidence

    def cache_icp_confidence_for_model_id(
        self,
        model_id: str,
        *,
        geometry_revision: int,
        confidence: Any,
    ) -> None:
        """현재 원본 revision의 ICP 점 분포 신뢰도를 모델 수명 동안 저장합니다."""

        normalized_model_id = str(model_id).strip()
        model = self.models.get(normalized_model_id)
        if model is None:
            raise ModelRepositoryError(
                f"ICP 점 분포 신뢰도를 저장할 모델이 없습니다: {normalized_model_id}"
            )
        current_revision = int(getattr(model, "geometry_revision", -1))
        if current_revision < 1 or int(geometry_revision) != current_revision:
            raise ModelRepositoryError("ICP 점 분포 신뢰도의 geometry revision이 현재 모델과 다릅니다.")
        self.icp_confidence_cache[normalized_model_id] = (current_revision, confidence)

    def contains_source_path(self, source_path: str | os.PathLike[str]) -> bool:
        """정규화한 원본 경로를 사용하는 레이어가 하나 이상 있는지 반환합니다."""
        return canonical_source_path(source_path) in self.source_path_index

    def model_id_for_source_path(self, source_path: str | os.PathLike[str]) -> str | None:
        """해당 원본 경로에서 가장 먼저 등록되어 남아 있는 모델 ID를 반환합니다."""
        return self.source_path_index.get(canonical_source_path(source_path))

    def geometry_kind_for_model_id(self, model_id: str) -> str:
        """등록 모델의 COLMAP·삼각형 메시 형상 종류를 반환합니다."""
        model = self.models.get(str(model_id))
        if model is None:
            raise ModelRepositoryError(f"형상 종류를 조회할 모델이 없습니다: {model_id}")
        if isinstance(model, TriangleMeshModelData):
            return GEOMETRY_KIND_TRIANGLE_MESH
        return GEOMETRY_KIND_COLMAP_POINT_CLOUD

    def unique_display_name(self, preferred_name: str) -> str:
        """동일 원본·폴더 이름에 `_2`, `_3`을 붙여 레이어 표시 이름을 구분합니다."""
        normalized_name = preferred_name.strip()
        if not normalized_name:
            raise ModelRepositoryError("COLMAP 모델 표시 이름이 비어 있습니다.")
        names = {model.display_name for model in self.models.values()}
        if normalized_name not in names:
            return normalized_name
        suffix = 2
        while f"{normalized_name}_{suffix}" in names:
            suffix += 1
        return f"{normalized_name}_{suffix}"

    def prepare_model_data(
        self,
        parsed_model: Any,
        source_path: str | os.PathLike[str],
        *,
        model_id_factory: Callable[[], str] = create_model_id,
        display_name_hint: str = "",
    ) -> ColmapModelData | TriangleMeshModelData:
        """원본 경로가 같아도 새 ID와 고유 표시 이름의 준비 모델을 생성합니다."""
        canonical_path = canonical_source_path(source_path)
        preferred_name = display_name_hint.strip() or Path(canonical_path).name
        display_name = self.unique_display_name(preferred_name)
        return build_model_data(
            parsed_model,
            canonical_path,
            model_id_factory=model_id_factory,
            display_name=display_name,
        )

    def add_model(
        self,
        model: ColmapModelData | TriangleMeshModelData,
        display_transform: ModelDisplayTransform,
        source_metadata: ImportedModelSourceMetadata | None = None,
    ) -> None:
        """원본 불변 버퍼와 정렬 검사를 준비한 뒤 독립 레이어를 원자 등록합니다."""
        if model.model_id in self.models:
            raise ModelRepositoryError(f"중복 COLMAP 모델 ID입니다: {model.model_id}")
        if model.model_id in self.display_transforms:
            raise ModelRepositoryError(
                f"COLMAP 모델 ID에 이미 표시 축 변환이 등록되어 있습니다: {model.model_id}"
            )
        if model.model_id in self.alignment_transforms:
            raise ModelRepositoryError(
                f"COLMAP 모델 ID에 이미 확정 alignment가 등록되어 있습니다: {model.model_id}"
            )
        if not isinstance(display_transform, ModelDisplayTransform):
            raise ModelRepositoryError("COLMAP 모델의 표시 축 변환 형식이 올바르지 않습니다.")
        if source_metadata is not None and not isinstance(source_metadata, ImportedModelSourceMetadata):
            raise ModelRepositoryError("COLMAP 모델 source metadata 형식이 올바르지 않습니다.")
        stored_path = os.path.normcase(os.path.normpath(str(model.source_path)))
        canonical_path = canonical_source_path(model.source_path)
        if stored_path != canonical_path:
            raise ModelRepositoryError("COLMAP 모델 소스 경로가 정규화되지 않았습니다.")
        if source_metadata is not None:
            if source_metadata.sparse_path_absolute != canonical_path:
                raise ModelRepositoryError("COLMAP source metadata 경로가 모델 경로와 다릅니다.")
            if (
                source_metadata.axis_preset != display_transform.preset_id
                or source_metadata.axis_forward != display_transform.forward_axis
                or source_metadata.axis_up != display_transform.up_axis
                or source_metadata.display_transform_fingerprint != display_transform.fingerprint
            ):
                raise ModelRepositoryError("COLMAP source metadata의 표시 축 snapshot이 모델과 다릅니다.")
        from .alignment.icp_prepared_input import seal_point_model, prepare_alignment_input
        model = seal_point_model(model)
        prepared_input = prepare_alignment_input(model)
        self.models[model.model_id] = model
        try:
            self.alignment_inputs[(model.model_id, 'MAIN')] = prepared_input
            self.source_path_index.setdefault(canonical_path, model.model_id)
            self.display_transforms[model.model_id] = display_transform
            self.alignment_transforms[model.model_id] = identity_sim3_transform()
            if source_metadata is not None:
                self.source_metadata[model.model_id] = source_metadata
        except Exception:
            self.models.pop(model.model_id, None)
            self.alignment_inputs.pop((model.model_id, 'MAIN'), None)
            self._remove_source_reference(canonical_path, model.model_id)
            self.display_transforms.pop(model.model_id, None)
            self.alignment_transforms.pop(model.model_id, None)
            self.source_metadata.pop(model.model_id, None)
            raise
        _LOGGER.info(
            "model_repository.add model_id=%s points=%s source=%s axis=%s alignment=%s",
            model.model_id,
            model_point_count(model),
            canonical_path,
            display_transform.fingerprint,
            self.alignment_transforms[model.model_id].fingerprint,
        )

    def add_companion_mesh(
        self,
        model_id: str,
        mesh: TriangleMeshModelData,
        source_metadata: CompanionMeshSourceMetadata,
        display_transform: ModelDisplayTransform | None = None,
    ) -> None:
        """연결 메시의 정렬 검사를 완료하고 원본·출처·독립 축을 원자 등록합니다."""
        owner = self.models.get(model_id)
        if not isinstance(owner, ColmapModelData):
            raise ModelRepositoryError("연결 메시를 소유할 COLMAP 모델이 없습니다.")
        if model_id in self.companion_meshes or model_id in self.companion_sources or model_id in self.companion_display_transforms:
            raise ModelRepositoryError("이 COLMAP 데이터셋에는 이미 연결 메시가 있습니다.")
        if not isinstance(mesh, TriangleMeshModelData) or mesh.model_id != model_id:
            raise ModelRepositoryError("연결 메시의 모델 ID가 COLMAP 소유자와 다릅니다.")
        if not isinstance(source_metadata, CompanionMeshSourceMetadata):
            raise ModelRepositoryError("연결 메시 출처 메타데이터가 올바르지 않습니다.")
        if canonical_source_path(mesh.source_path) != source_metadata.source_path_absolute:
            raise ModelRepositoryError("연결 메시 원본 경로와 출처 메타데이터가 다릅니다.")
        if mesh.materials or mesh.texture_coordinate_count:
            raise ModelRepositoryError("연결 메시 프록시에는 UV·재질을 포함할 수 없습니다.")
        transform = self.display_transform_for_model_id(model_id) if display_transform is None else display_transform
        if not isinstance(transform, ModelDisplayTransform):
            raise ModelRepositoryError("연결 메시의 원본 축 설정이 올바르지 않습니다.")
        from .alignment.icp_prepared_input import prepare_alignment_input
        prepared_input = prepare_alignment_input(mesh)
        self.companion_meshes[model_id] = mesh
        try:
            self.alignment_inputs[(model_id, 'COMPANION')] = prepared_input
            self.companion_sources[model_id] = source_metadata
            self.companion_display_transforms[model_id] = transform
        except Exception:
            self.companion_meshes.pop(model_id, None)
            self.alignment_inputs.pop((model_id, 'COMPANION'), None)
            self.companion_sources.pop(model_id, None)
            self.companion_display_transforms.pop(model_id, None)
            raise

    def companion_mesh_for_model_id(self, model_id: str) -> TriangleMeshModelData | None:
        """해당 COLMAP이 소유하는 선택적 연결 메시를 반환합니다."""
        return self.companion_meshes.get(model_id)

    def source_metadata_for_model_id(self, model_id: str) -> ImportedModelSourceMetadata:
        """저장 가능한 등록 모델의 검증 완료 source metadata를 반환합니다."""
        try:
            return self.source_metadata[model_id]
        except KeyError as error:
            raise ModelRepositoryError(
                f"COLMAP 모델 source metadata가 없어 프로젝트에 저장할 수 없습니다: {model_id}"
            ) from error

    def display_transform_for_model_id(self, model_id: str) -> ModelDisplayTransform:
        """등록 모델의 표시 축 변환을 누락 없이 반환합니다."""
        try:
            return self.display_transforms[model_id]
        except KeyError as error:
            raise ModelRepositoryError(
                f"COLMAP 모델의 표시 축 변환을 찾을 수 없습니다: {model_id}"
            ) from error

    def set_display_transform(self, model_id: str, transform: ModelDisplayTransform) -> None:
        """최초 원본 출처를 유지하며 주 형상의 현재 축 정규화 값만 교체합니다."""
        if model_id not in self.models or model_id not in self.display_transforms:
            raise ModelRepositoryError("축 설정을 변경할 등록 모델이 없습니다.")
        if not isinstance(transform, ModelDisplayTransform):
            raise ModelRepositoryError("주 모델의 축 설정 형식이 올바르지 않습니다.")
        self.display_transforms[model_id] = transform

    def companion_display_transform_for_model_id(self, model_id: str) -> ModelDisplayTransform:
        """COLMAP 소유 연결 메시의 현재 축 정규화 값을 반환합니다."""
        if model_id not in self.companion_meshes:
            raise ModelRepositoryError("축을 조회할 연결 메시가 없습니다.")
        try:
            return self.companion_display_transforms[model_id]
        except KeyError as error:
            raise ModelRepositoryError("연결 메시의 축 설정이 누락되었습니다.") from error

    def set_companion_display_transform(self, model_id: str, transform: ModelDisplayTransform) -> None:
        """주 형상과 공통 정렬을 유지하며 연결 메시의 현재 축만 교체합니다."""
        self.companion_display_transform_for_model_id(model_id)
        if not isinstance(transform, ModelDisplayTransform):
            raise ModelRepositoryError("연결 메시의 축 설정 형식이 올바르지 않습니다.")
        self.companion_display_transforms[model_id] = transform

    def alignment_transform_for_model_id(self, model_id: str) -> Sim3Transform:
        """등록 모델의 확정 Sim(3) alignment를 누락 없이 반환합니다."""
        try:
            return self.alignment_transforms[model_id]
        except KeyError as error:
            raise ModelRepositoryError(
                f"COLMAP 모델의 확정 alignment를 찾을 수 없습니다: {model_id}"
            ) from error

    def set_alignment_transform(self, model_id: str, transform: Sim3Transform) -> None:
        """후속 승인 단계가 사용할 확정 Sim(3)을 등록 모델에 원자적으로 기록합니다."""
        if model_id not in self.models:
            raise ModelRepositoryError(f"확정 alignment를 기록할 COLMAP 모델이 없습니다: {model_id}")
        if not isinstance(transform, Sim3Transform):
            raise ModelRepositoryError("확정 alignment 형식은 Sim3Transform이어야 합니다.")
        if model_id not in self.display_transforms:
            raise ModelRepositoryError("확정 alignment를 기록할 모델의 표시 축 변환이 없습니다.")
        self.alignment_transforms[model_id] = transform
        _LOGGER.info(
            "model_repository.set_alignment model_id=%s alignment=%s",
            model_id,
            transform.fingerprint,
        )

    def compare_and_set_alignment_transform(
        self,
        model_id: str,
        *,
        expected_fingerprint: str,
        transform: Sim3Transform,
    ) -> bool:
        """승인 직전의 확정 alignment가 snapshot과 같을 때만 새 값을 기록합니다."""

        normalized_model_id = str(model_id).strip()
        expected = str(expected_fingerprint).strip()
        if not normalized_model_id:
            raise ModelRepositoryError("확정 alignment를 비교할 모델 ID가 비어 있습니다.")
        if not expected:
            raise ModelRepositoryError("확정 alignment 비교 fingerprint가 비어 있습니다.")
        current = self.alignment_transform_for_model_id(normalized_model_id)
        if current.fingerprint != expected:
            return False
        self.set_alignment_transform(normalized_model_id, transform)
        return True

    def _remove_source_reference(self, canonical_path: str, model_id: str) -> None:
        """삭제한 모델이 경로 대표였다면 같은 원본의 다음 생존 모델로 교체합니다."""
        if self.source_path_index.get(canonical_path) != model_id:
            return
        replacement = next(
            (
                remaining_id
                for remaining_id, remaining in self.models.items()
                if os.path.normcase(os.path.normpath(str(remaining.source_path))) == canonical_path
            ),
            None,
        )
        if replacement is None:
            self.source_path_index.pop(canonical_path, None)
        else:
            self.source_path_index[canonical_path] = replacement

    def remove_model(self, model_id: str) -> ColmapModelData | TriangleMeshModelData | None:
        """해당 레이어와 준비 입력의 소유권을 해제하고 다른 레이어는 유지합니다."""
        model = self.models.pop(model_id, None)
        if model is None:
            return None
        self._remove_source_reference(canonical_source_path(model.source_path), model_id)
        self.display_transforms.pop(model_id, None)
        self.alignment_transforms.pop(model_id, None)
        self.source_metadata.pop(model_id, None)
        self.icp_confidence_cache.pop(model_id, None)
        self.companion_meshes.pop(model_id, None)
        self.companion_sources.pop(model_id, None)
        self.companion_display_transforms.pop(model_id, None)
        self.alignment_inputs.pop((model_id, 'MAIN'), None)
        self.alignment_inputs.pop((model_id, 'COMPANION'), None)
        _LOGGER.info("model_repository.remove model_id=%s", model_id)
        return model

    def clear(self) -> None:
        """파일 로드·해제 전에 원본·연결 메시와 준비 입력의 소유권을 해제합니다."""
        if self.models:
            _LOGGER.info("model_repository.clear model_count=%s", len(self.models))
        self.models.clear()
        self.source_path_index.clear()
        self.display_transforms.clear()
        self.alignment_transforms.clear()
        self.source_metadata.clear()
        self.icp_confidence_cache.clear()
        self.companion_meshes.clear()
        self.companion_sources.clear()
        self.companion_display_transforms.clear()
        self.alignment_inputs.clear()


# 기존 import 호환성을 유지하면서 데이터 종류 중립 이름도 제공합니다.
ModelRepository = ColmapModelRepository
