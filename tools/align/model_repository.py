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
    """같은 sparse model 폴더의 중복 등록 요청을 나타냅니다."""


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
        if (
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
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


def canonical_source_path(source_path: str | os.PathLike[str]) -> str:
    """중복 탐지용 sparse model 폴더 경로를 플랫폼 규칙으로 정규화합니다."""
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
    """검증된 COLMAP·메시 모델에 런타임 ID·표시 이름·정규 경로를 원자적으로 부여합니다."""
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
    return replace(
        parsed_model,
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
    """대규모 실제 모델과 정규화된 소스 경로 색인을 소유합니다."""

    models: dict[str, ColmapModelData | TriangleMeshModelData]
    source_path_index: dict[str, str]
    display_transforms: dict[str, ModelDisplayTransform]
    alignment_transforms: dict[str, Sim3Transform]
    source_metadata: dict[str, ImportedModelSourceMetadata]
    icp_confidence_cache: dict[str, tuple[int, Any]]

    def __init__(self) -> None:
        """비어 있는 실제 모델 저장소와 파생 ICP 평가 캐시를 초기화합니다."""
        self.models = {}
        self.source_path_index = {}
        self.display_transforms = {}
        self.alignment_transforms = {}
        self.source_metadata = {}
        self.icp_confidence_cache = {}

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
        """정규화한 sparse model 폴더가 이미 등록됐는지 반환합니다."""
        return canonical_source_path(source_path) in self.source_path_index

    def model_id_for_source_path(self, source_path: str | os.PathLike[str]) -> str | None:
        """정규화한 sparse model 폴더에 연결된 런타임 모델 ID를 반환합니다."""
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
        """동일 폴더 이름에 `_2`, `_3` 접미사를 붙여 표시 이름을 고유하게 만듭니다."""
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
        """중복 소스를 미리 거부하고 고유 표시 이름의 staged 모델을 생성합니다."""
        canonical_path = canonical_source_path(source_path)
        existing_model_id = self.source_path_index.get(canonical_path)
        if existing_model_id is not None:
            raise DuplicateModelSourceError(
                f"같은 COLMAP 모델 폴더를 이미 불러왔습니다: {canonical_path}"
            )
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
        """모델·경로·축·alignment와 선택 source metadata를 하나의 원자 단위로 등록합니다."""
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
        if canonical_path in self.source_path_index:
            raise DuplicateModelSourceError(
                f"같은 COLMAP 모델 폴더를 이미 불러왔습니다: {canonical_path}"
            )
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
        self.models[model.model_id] = model
        try:
            self.source_path_index[canonical_path] = model.model_id
            self.display_transforms[model.model_id] = display_transform
            self.alignment_transforms[model.model_id] = identity_sim3_transform()
            if source_metadata is not None:
                self.source_metadata[model.model_id] = source_metadata
        except Exception:
            self.models.pop(model.model_id, None)
            self.source_path_index.pop(canonical_path, None)
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

    def remove_model(self, model_id: str) -> ColmapModelData | TriangleMeshModelData | None:
        """런타임 모델·변환·source metadata·파생 ICP 평가 캐시를 함께 제거합니다."""
        model = self.models.pop(model_id, None)
        if model is None:
            return None
        self.source_path_index.pop(canonical_source_path(model.source_path), None)
        self.display_transforms.pop(model_id, None)
        self.alignment_transforms.pop(model_id, None)
        self.source_metadata.pop(model_id, None)
        self.icp_confidence_cache.pop(model_id, None)
        _LOGGER.info("model_repository.remove model_id=%s", model_id)
        return model

    def clear(self) -> None:
        """파일 로드나 unregister 전에 모델 배열·경로·파생 평가 캐시를 비웁니다."""
        if self.models:
            _LOGGER.info("model_repository.clear model_count=%s", len(self.models))
        self.models.clear()
        self.source_path_index.clear()
        self.display_transforms.clear()
        self.alignment_transforms.clear()
        self.source_metadata.clear()
        self.icp_confidence_cache.clear()


# 기존 import 호환성을 유지하면서 데이터 종류 중립 이름도 제공합니다.
ModelRepository = ColmapModelRepository
