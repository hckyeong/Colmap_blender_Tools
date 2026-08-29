"""COLMAP 구조 통합 내보내기의 작은 불변 입력·결과 타입을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from math import isfinite
from numbers import Integral, Real
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np
import numpy.typing as npt

from ..constants import GEOMETRY_KIND_COLMAP_POINT_CLOUD


Float64Matrix = npt.NDArray[np.float64]


class MergeExportError(RuntimeError):
    """구조 통합 내보내기에서 사용자에게 전달할 오류를 구분합니다."""


class MergePreflightError(MergeExportError):
    """기록 전에 발견된 입력 구조 또는 정책 위반을 나타냅니다."""


class MergeProtocolError(MergeExportError):
    """worker 작업 파일의 형식 또는 식별자 위반을 나타냅니다."""


class MergeCancelledError(MergeExportError):
    """작업 취소 sentinel을 확인한 worker가 중단되었음을 나타냅니다."""


class MergeStaleResultError(MergeExportError):
    """worker 실행 뒤 메인 Runtime 입력이 달라졌음을 나타냅니다."""


class MergePhase(str, Enum):
    """메인 Runtime과 worker가 공유하는 작은 작업 상태를 정의합니다."""

    IDLE = "IDLE"
    PREFLIGHT_RUNNING = "PREFLIGHT_RUNNING"
    PREFLIGHT_READY = "PREFLIGHT_READY"
    EXPORT_RUNNING = "EXPORT_RUNNING"
    CANCEL_PENDING = "CANCEL_PENDING"
    WAITING_FOR_FINALIZE = "WAITING_FOR_FINALIZE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


class MergeOutputFormat(str, Enum):
    """최종 sparse model에 기록할 COLMAP 파일 형식을 정의합니다."""

    TEXT = "TEXT"
    BINARY = "BINARY"


class MergeStage(str, Enum):
    """worker 진행률에 기록할 고정 단계 이름을 정의합니다."""

    SNAPSHOT_INPUTS = "SNAPSHOT_INPUTS"
    FINGERPRINT_SOURCES = "FINGERPRINT_SOURCES"
    READ_AND_VALIDATE_SOURCES = "READ_AND_VALIDATE_SOURCES"
    NORMALIZE_RIGS_AND_FRAMES = "NORMALIZE_RIGS_AND_FRAMES"
    BAKE_PROJECT_WORLD = "BAKE_PROJECT_WORLD"
    PREFLIGHT_DUPLICATE_IMAGES = "PREFLIGHT_DUPLICATE_IMAGES"
    REMAP_RIGS_CAMERAS_FRAMES = "REMAP_RIGS_CAMERAS_FRAMES"
    MERGE_IMAGES_BY_BASENAME = "MERGE_IMAGES_BY_BASENAME"
    BUILD_OBSERVATION_KEYS = "BUILD_OBSERVATION_KEYS"
    MERGE_POINT_TRACKS = "MERGE_POINT_TRACKS"
    REPROJECT_MERGED_POINTS = "REPROJECT_MERGED_POINTS"
    BUILD_BIDIRECTIONAL_LINKS = "BUILD_BIDIRECTIONAL_LINKS"
    WRITE_TEXT_MODEL = "WRITE_TEXT_MODEL"
    WRITE_BINARY_MODEL = "WRITE_BINARY_MODEL"
    ROUNDTRIP_VALIDATE = "ROUNDTRIP_VALIDATE"
    VALIDATE_SOURCE_IMAGES = "VALIDATE_SOURCE_IMAGES"
    COPY_IMAGES = "COPY_IMAGES"
    WRITE_AUDIT_REPORTS = "WRITE_AUDIT_REPORTS"
    WAITING_FOR_FINALIZE = "WAITING_FOR_FINALIZE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _nonempty_text(value: object, field_name: str) -> str:
    """비어 있지 않은 문자열만 불변 타입에 넣도록 정규화합니다."""

    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name}이(가) 비어 있습니다.")
    return text


def _freeze_matrix(value: object, field_name: str) -> Float64Matrix:
    """4×4 float64 행렬을 유한·연속·읽기 전용 상태로 고정합니다."""

    try:
        matrix = np.ascontiguousarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{field_name}이(가) float64 행렬이 아닙니다.") from error
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError(f"{field_name}은(는) 유한한 4×4 행렬이어야 합니다.")
    matrix.setflags(write=False)
    return matrix


def _freeze_mapping(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, Any] | None:
    """작은 감사 mapping을 외부 변경에서 분리합니다."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name}은(는) mapping 또는 None이어야 합니다.")
    return MappingProxyType(dict(value))


def _strict_bool(value: object, field_name: str) -> bool:
    """문자열·정수의 진릿값 변환 없이 실제 bool만 IPC에 허용합니다."""

    if not isinstance(value, bool):
        raise ValueError(f"{field_name}은(는) bool이어야 합니다.")
    return value


def _json_value(value: Any) -> Any:
    """불변 타입의 값을 JSON 기본 타입으로 엄격하게 바꿉니다."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        if isinstance(value, float) and not isfinite(value):
            raise MergeProtocolError("내보내기 JSON에 유한하지 않은 수치가 있습니다.")
        return value
    raise MergeProtocolError("내보내기 JSON에 기록할 수 없는 값이 있습니다.")


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """추측성 허용 대신 보수적 기본값을 사용하는 병합 정책입니다."""

    duplicate_point2d_max_pixel_error: float = 0.0
    duplicate_camera_param_rtol: float = 0.0
    duplicate_camera_param_atol: float = 0.0
    duplicate_pose_max_rotation_degrees: float = 0.0
    duplicate_pose_max_center_distance: float = 0.0
    merged_point_max_reprojection_error_pixels: float | None = None
    require_roundtrip_validation: bool = True

    def __post_init__(self) -> None:
        """모든 허용 오차가 명시적이고 음수가 아닌지 검증합니다."""

        values = (
            self.duplicate_point2d_max_pixel_error,
            self.duplicate_camera_param_rtol,
            self.duplicate_camera_param_atol,
            self.duplicate_pose_max_rotation_degrees,
            self.duplicate_pose_max_center_distance,
        )
        if any(not isfinite(float(value)) or float(value) < 0.0 for value in values):
            raise ValueError("중복 이미지 허용 오차는 유한한 0 이상 수치여야 합니다.")
        reprojection = self.merged_point_max_reprojection_error_pixels
        if reprojection is not None and (
            not isfinite(float(reprojection)) or float(reprojection) < 0.0
        ):
            raise ValueError("재투영 오차 한도는 None 또는 유한한 0 이상 수치여야 합니다.")
        if not isinstance(self.require_roundtrip_validation, bool):
            raise ValueError("왕복 검증 사용 여부는 bool이어야 합니다.")

    def to_mapping(self) -> dict[str, Any]:
        """worker manifest에 넣을 결정적 JSON mapping을 반환합니다."""

        return _json_value({field.name: getattr(self, field.name) for field in self.__dataclass_fields__.values()})

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergePolicy":
        """manifest의 명시 정책만 엄격히 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("병합 정책은 JSON 객체여야 합니다.")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value).difference(allowed)
        if unknown:
            raise MergeProtocolError(f"알 수 없는 병합 정책 항목이 있습니다: {', '.join(sorted(unknown))}")
        try:
            return cls(**dict(value))
        except (TypeError, ValueError) as error:
            raise MergeProtocolError("병합 정책 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeSourceModelSnapshot:
    """A 그룹의 한 모델을 worker가 다시 적재할 최소 입력만 보관합니다."""

    model_id: str
    display_name: str
    dataset_root: Path
    source_path: Path
    source_format: str
    catalog_load_order: int
    geometry_revision: int
    is_anchor: bool
    display_matrix_float64: Float64Matrix
    display_fingerprint: str
    alignment_matrix_float64: Float64Matrix
    alignment_fingerprint: str
    expected_source_fingerprint: str
    alignment_provenance: Mapping[str, Any] | None = None
    geometry_kind: str = GEOMETRY_KIND_COLMAP_POINT_CLOUD

    def __post_init__(self) -> None:
        """COLMAP source 메타데이터와 두 확정 변환을 즉시 검증합니다."""

        model_id = _nonempty_text(self.model_id, "모델 ID")
        display_name = _nonempty_text(self.display_name, "모델 표시 이름")
        source_format = _nonempty_text(self.source_format, "COLMAP 소스 형식").lower()
        if source_format not in {"binary", "text"}:
            raise ValueError("COLMAP 소스 형식은 binary 또는 text여야 합니다.")
        geometry_kind = str(self.geometry_kind).strip().upper() or GEOMETRY_KIND_COLMAP_POINT_CLOUD
        if geometry_kind != GEOMETRY_KIND_COLMAP_POINT_CLOUD:
            raise ValueError("구조 통합 내보내기는 COLMAP 점군만 포함할 수 있습니다.")
        if isinstance(self.catalog_load_order, bool) or int(self.catalog_load_order) < 0:
            raise ValueError("카탈로그 load order는 0 이상의 정수여야 합니다.")
        if isinstance(self.geometry_revision, bool) or int(self.geometry_revision) < 0:
            raise ValueError("geometry revision은 0 이상의 정수여야 합니다.")
        is_anchor = _strict_bool(self.is_anchor, "Anchor 여부")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "dataset_root", Path(self.dataset_root))
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "source_format", source_format)
        object.__setattr__(self, "geometry_kind", geometry_kind)
        object.__setattr__(self, "catalog_load_order", int(self.catalog_load_order))
        object.__setattr__(self, "geometry_revision", int(self.geometry_revision))
        object.__setattr__(self, "is_anchor", is_anchor)
        object.__setattr__(
            self,
            "display_matrix_float64",
            _freeze_matrix(self.display_matrix_float64, "display_matrix_float64"),
        )
        object.__setattr__(
            self,
            "alignment_matrix_float64",
            _freeze_matrix(self.alignment_matrix_float64, "alignment_matrix_float64"),
        )
        object.__setattr__(self, "display_fingerprint", _nonempty_text(self.display_fingerprint, "display fingerprint"))
        object.__setattr__(self, "alignment_fingerprint", _nonempty_text(self.alignment_fingerprint, "alignment fingerprint"))
        expected_source_fingerprint = _nonempty_text(
            self.expected_source_fingerprint,
            "예상 source fingerprint",
        ).lower()
        if (
            len(expected_source_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in expected_source_fingerprint)
        ):
            raise ValueError("예상 source fingerprint가 SHA-256 형식이 아닙니다.")
        object.__setattr__(self, "expected_source_fingerprint", expected_source_fingerprint)
        object.__setattr__(
            self,
            "alignment_provenance",
            _freeze_mapping(self.alignment_provenance, "alignment provenance"),
        )

    @property
    def project_world_matrix_float64(self) -> Float64Matrix:
        """Alignment × Display Axis 순서로 한 번만 합성한 Project World 행렬을 반환합니다."""

        matrix = np.ascontiguousarray(
            self.alignment_matrix_float64 @ self.display_matrix_float64,
            dtype=np.float64,
        )
        matrix.setflags(write=False)
        return matrix

    @property
    def provenance_is_unknown(self) -> bool:
        """기존 세션의 누락 provenance를 감사 경고로 표시할지 반환합니다."""

        if self.alignment_provenance is None:
            return True
        return str(self.alignment_provenance.get("method", "UNKNOWN")).strip().upper() == "UNKNOWN"

    def to_mapping(self) -> dict[str, Any]:
        """worker manifest용 source snapshot mapping을 반환합니다."""

        return _json_value(
            {
                "model_id": self.model_id,
                "display_name": self.display_name,
                "dataset_root": self.dataset_root,
                "source_path": self.source_path,
                "source_format": self.source_format,
                "geometry_kind": self.geometry_kind,
                "catalog_load_order": self.catalog_load_order,
                "geometry_revision": self.geometry_revision,
                "is_anchor": self.is_anchor,
                "display_matrix_float64": self.display_matrix_float64,
                "display_fingerprint": self.display_fingerprint,
                "alignment_matrix_float64": self.alignment_matrix_float64,
                "alignment_fingerprint": self.alignment_fingerprint,
                "expected_source_fingerprint": self.expected_source_fingerprint,
                "alignment_provenance": self.alignment_provenance,
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeSourceModelSnapshot":
        """worker manifest의 source 항목을 엄격한 불변 타입으로 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("source model snapshot은 JSON 객체여야 합니다.")
        required = {
            "model_id",
            "display_name",
            "dataset_root",
            "source_path",
            "source_format",
            "catalog_load_order",
            "geometry_revision",
            "is_anchor",
            "display_matrix_float64",
            "display_fingerprint",
            "alignment_matrix_float64",
            "alignment_fingerprint",
            "expected_source_fingerprint",
        }
        missing = required.difference(value)
        if missing:
            raise MergeProtocolError(f"source model snapshot 필수 항목이 없습니다: {', '.join(sorted(missing))}")
        try:
            return cls(
                model_id=str(value["model_id"]),
                display_name=str(value["display_name"]),
                dataset_root=Path(str(value["dataset_root"])),
                source_path=Path(str(value["source_path"])),
                source_format=str(value["source_format"]),
                geometry_kind=str(value.get("geometry_kind", GEOMETRY_KIND_COLMAP_POINT_CLOUD)),
                catalog_load_order=int(value["catalog_load_order"]),
                geometry_revision=int(value["geometry_revision"]),
                is_anchor=_strict_bool(value["is_anchor"], "source model snapshot is_anchor"),
                display_matrix_float64=value["display_matrix_float64"],
                display_fingerprint=str(value["display_fingerprint"]),
                alignment_matrix_float64=value["alignment_matrix_float64"],
                alignment_fingerprint=str(value["alignment_fingerprint"]),
                expected_source_fingerprint=str(value["expected_source_fingerprint"]),
                alignment_provenance=value.get("alignment_provenance"),
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise MergeProtocolError("source model snapshot 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeExportSnapshot:
    """A 그룹을 기준으로 고정한 worker 입력과 main stale 비교 계약입니다."""

    job_id: str
    protocol_version: str
    algorithm_version: str
    output_root: Path
    anchor_model_id: str
    source_models: tuple[MergeSourceModelSnapshot, ...]
    runtime_fingerprint: str
    output_format: MergeOutputFormat = MergeOutputFormat.TEXT
    restore_import_axes: bool = True
    overwrite_existing_output: bool = False
    merge_policy: MergePolicy = field(default_factory=MergePolicy)
    candidate_root: Path | None = None
    excluded_model_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """A 그룹 snapshot의 anchor·모델 고유성·결정적 순서를 검증합니다."""

        sources = tuple(self.source_models)
        if not sources:
            raise ValueError("A 그룹에 내보낼 모델이 없습니다.")
        if not all(isinstance(source, MergeSourceModelSnapshot) for source in sources):
            raise ValueError("source_models에는 MergeSourceModelSnapshot만 넣을 수 있습니다.")
        model_ids = tuple(source.model_id for source in sources)
        if len(set(model_ids)) != len(model_ids):
            raise ValueError("내보내기 source model ID가 중복되었습니다.")
        anchor_model_id = _nonempty_text(self.anchor_model_id, "Anchor 모델 ID")
        anchors = tuple(source for source in sources if source.is_anchor)
        if len(anchors) != 1 or anchors[0].model_id != anchor_model_id:
            raise ValueError("A 그룹 snapshot에는 정확히 한 개의 Anchor가 필요합니다.")
        ordered = tuple(
            sorted(
                sources,
                key=lambda source: (
                    0 if source.is_anchor else 1,
                    source.catalog_load_order,
                    source.model_id,
                ),
            )
        )
        if not isinstance(self.merge_policy, MergePolicy):
            raise ValueError("merge_policy 형식이 올바르지 않습니다.")
        try:
            output_format = MergeOutputFormat(self.output_format)
        except ValueError as error:
            raise ValueError("내보내기 sparse 형식이 올바르지 않습니다.") from error
        output_root = Path(self.output_root)
        if not str(output_root):
            raise ValueError("내보내기 대상 경로가 비어 있습니다.")
        candidate_root = Path(self.candidate_root) if self.candidate_root is not None else None
        excluded_model_ids = tuple(_nonempty_text(item, "제외 모델 ID") for item in self.excluded_model_ids)
        if len(set(excluded_model_ids)) != len(excluded_model_ids):
            raise ValueError("제외 모델 ID가 중복되었습니다.")
        if set(model_ids).intersection(excluded_model_ids):
            raise ValueError("포함·제외 모델 ID가 겹칩니다.")
        object.__setattr__(self, "job_id", _nonempty_text(self.job_id, "내보내기 job ID"))
        object.__setattr__(self, "protocol_version", _nonempty_text(self.protocol_version, "내보내기 protocol version"))
        object.__setattr__(self, "algorithm_version", _nonempty_text(self.algorithm_version, "내보내기 algorithm version"))
        object.__setattr__(self, "output_root", output_root)
        object.__setattr__(self, "anchor_model_id", anchor_model_id)
        object.__setattr__(self, "source_models", ordered)
        object.__setattr__(self, "runtime_fingerprint", _nonempty_text(self.runtime_fingerprint, "Runtime fingerprint"))
        object.__setattr__(self, "output_format", output_format)
        object.__setattr__(
            self,
            "restore_import_axes",
            _strict_bool(self.restore_import_axes, "import 축변환 복원 여부"),
        )
        object.__setattr__(
            self,
            "overwrite_existing_output",
            _strict_bool(self.overwrite_existing_output, "기존 출력 덮어쓰기 승인"),
        )
        object.__setattr__(self, "candidate_root", candidate_root)
        object.__setattr__(self, "excluded_model_ids", excluded_model_ids)

    def with_candidate_root(self, candidate_root: Path) -> "MergeExportSnapshot":
        """backend가 만든 형제 후보 폴더를 포함한 새 snapshot을 반환합니다."""

        return replace(self, candidate_root=Path(candidate_root))

    def to_mapping(self) -> dict[str, Any]:
        """worker manifest에 쓸 전체 immutable snapshot mapping을 반환합니다."""

        return _json_value(
            {
                "job_id": self.job_id,
                "protocol_version": self.protocol_version,
                "algorithm_version": self.algorithm_version,
                "output_root": self.output_root,
                "candidate_root": self.candidate_root,
                "excluded_model_ids": self.excluded_model_ids,
                "anchor_model_id": self.anchor_model_id,
                "source_models": [source.to_mapping() for source in self.source_models],
                "runtime_fingerprint": self.runtime_fingerprint,
                "output_format": self.output_format,
                "restore_import_axes": self.restore_import_axes,
                "overwrite_existing_output": self.overwrite_existing_output,
                "merge_policy": self.merge_policy.to_mapping(),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeExportSnapshot":
        """manifest의 전체 snapshot을 엄격히 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("내보내기 manifest는 JSON 객체여야 합니다.")
        required = {
            "job_id",
            "protocol_version",
            "algorithm_version",
            "output_root",
            "anchor_model_id",
            "source_models",
            "runtime_fingerprint",
            "output_format",
            "merge_policy",
        }
        missing = required.difference(value)
        if missing:
            raise MergeProtocolError(f"내보내기 manifest 필수 항목이 없습니다: {', '.join(sorted(missing))}")
        sources = value["source_models"]
        if not isinstance(sources, list):
            raise MergeProtocolError("source_models는 JSON 배열이어야 합니다.")
        try:
            return cls(
                job_id=str(value["job_id"]),
                protocol_version=str(value["protocol_version"]),
                algorithm_version=str(value["algorithm_version"]),
                output_root=Path(str(value["output_root"])),
                candidate_root=(
                    Path(str(value["candidate_root"]))
                    if value.get("candidate_root") is not None
                    else None
                ),
                excluded_model_ids=tuple(str(item) for item in value.get("excluded_model_ids", ())),
                anchor_model_id=str(value["anchor_model_id"]),
                source_models=tuple(MergeSourceModelSnapshot.from_mapping(item) for item in sources),
                runtime_fingerprint=str(value["runtime_fingerprint"]),
                output_format=MergeOutputFormat(str(value["output_format"])),
                restore_import_axes=_strict_bool(
                    value.get("restore_import_axes", True),
                    "manifest import 축변환 복원 여부",
                ),
                overwrite_existing_output=_strict_bool(
                    value.get("overwrite_existing_output", False),
                    "manifest 기존 출력 덮어쓰기 승인",
                ),
                merge_policy=MergePolicy.from_mapping(value["merge_policy"]),
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise MergeProtocolError("내보내기 manifest 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeIssue:
    """UI가 원인·영향·해결을 함께 표시할 수 있는 한 건의 진단입니다."""

    code: str
    cause: str
    impact: str
    remedy: str
    model_id: str | None = None

    def __post_init__(self) -> None:
        """빈 진단 문구를 막아 오류를 사용자에게 숨기지 않습니다."""

        object.__setattr__(self, "code", _nonempty_text(self.code, "진단 코드"))
        object.__setattr__(self, "cause", _nonempty_text(self.cause, "진단 원인"))
        object.__setattr__(self, "impact", _nonempty_text(self.impact, "진단 영향"))
        object.__setattr__(self, "remedy", _nonempty_text(self.remedy, "진단 해결 방법"))
        if self.model_id is not None:
            object.__setattr__(self, "model_id", _nonempty_text(self.model_id, "진단 모델 ID"))

    def to_mapping(self) -> dict[str, Any]:
        """progress/result JSON에 쓸 작은 진단 mapping을 반환합니다."""

        return _json_value(
            {
                "code": self.code,
                "cause": self.cause,
                "impact": self.impact,
                "remedy": self.remedy,
                "model_id": self.model_id,
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeIssue":
        """JSON 진단을 UI용 불변 타입으로 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("내보내기 진단은 JSON 객체여야 합니다.")
        try:
            return cls(
                code=str(value["code"]),
                cause=str(value["cause"]),
                impact=str(value["impact"]),
                remedy=str(value["remedy"]),
                model_id=(str(value["model_id"]) if value.get("model_id") is not None else None),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MergeProtocolError("내보내기 진단 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeExportPreflightResult:
    """기록 전에 worker가 계산한 안전한 입력 검사 요약입니다."""

    job_id: str
    runtime_fingerprint: str
    included_model_ids: tuple[str, ...]
    excluded_model_ids: tuple[str, ...] = ()
    duplicate_image_group_count: int = 0
    warnings: tuple[MergeIssue, ...] = ()
    errors: tuple[MergeIssue, ...] = ()

    def __post_init__(self) -> None:
        """preflight의 ID 집합과 count가 안정적으로 직렬화되는지 검증합니다."""

        included = tuple(_nonempty_text(item, "포함 모델 ID") for item in self.included_model_ids)
        excluded = tuple(_nonempty_text(item, "제외 모델 ID") for item in self.excluded_model_ids)
        if not included or len(set(included)) != len(included):
            raise ValueError("preflight 포함 모델 목록이 비어 있거나 중복되었습니다.")
        if set(included).intersection(excluded):
            raise ValueError("preflight 포함·제외 모델 목록이 겹칩니다.")
        if int(self.duplicate_image_group_count) < 0:
            raise ValueError("중복 이미지 그룹 수는 음수일 수 없습니다.")
        if not all(isinstance(item, MergeIssue) for item in (*self.warnings, *self.errors)):
            raise ValueError("preflight 진단 형식이 올바르지 않습니다.")
        object.__setattr__(self, "job_id", _nonempty_text(self.job_id, "preflight job ID"))
        object.__setattr__(self, "runtime_fingerprint", _nonempty_text(self.runtime_fingerprint, "preflight Runtime fingerprint"))
        object.__setattr__(self, "included_model_ids", included)
        object.__setattr__(self, "excluded_model_ids", excluded)
        object.__setattr__(self, "duplicate_image_group_count", int(self.duplicate_image_group_count))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def is_ready(self) -> bool:
        """치명 오류가 전혀 없어 기록 작업을 시작할 수 있는지 반환합니다."""

        return not self.errors

    def to_mapping(self) -> dict[str, Any]:
        """preflight.json에 쓸 결정적 JSON mapping을 반환합니다."""

        return _json_value(
            {
                "job_id": self.job_id,
                "runtime_fingerprint": self.runtime_fingerprint,
                "included_model_ids": self.included_model_ids,
                "excluded_model_ids": self.excluded_model_ids,
                "duplicate_image_group_count": self.duplicate_image_group_count,
                "warnings": [item.to_mapping() for item in self.warnings],
                "errors": [item.to_mapping() for item in self.errors],
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeExportPreflightResult":
        """worker preflight JSON을 엄격한 결과 타입으로 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("preflight 결과는 JSON 객체여야 합니다.")
        try:
            return cls(
                job_id=str(value["job_id"]),
                runtime_fingerprint=str(value["runtime_fingerprint"]),
                included_model_ids=tuple(str(item) for item in value["included_model_ids"]),
                excluded_model_ids=tuple(str(item) for item in value.get("excluded_model_ids", ())),
                duplicate_image_group_count=int(value.get("duplicate_image_group_count", 0)),
                warnings=tuple(MergeIssue.from_mapping(item) for item in value.get("warnings", ())),
                errors=tuple(MergeIssue.from_mapping(item) for item in value.get("errors", ())),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise MergeProtocolError("preflight 결과 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeExportProgress:
    """main Blender가 poll로 읽는 작고 검증된 진행률 상태입니다."""

    job_id: str
    stage: MergeStage | str
    fraction: float
    message: str
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """worker가 쓰는 진행률 수치·count와 단계 이름을 검증합니다."""

        try:
            stage = MergeStage(self.stage)
        except ValueError as error:
            raise ValueError("내보내기 진행 단계가 올바르지 않습니다.") from error
        if isinstance(self.fraction, bool) or not isinstance(self.fraction, Real):
            raise ValueError("내보내기 진행률은 수치여야 합니다.")
        fraction = float(self.fraction)
        if not isfinite(fraction) or not 0.0 <= fraction <= 1.0:
            raise ValueError("내보내기 진행률은 0과 1 사이의 유한 수치여야 합니다.")
        if not isinstance(self.counts, Mapping):
            raise ValueError("내보내기 진행 count는 mapping이어야 합니다.")
        counts: dict[str, int] = {}
        for key, value in self.counts.items():
            if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
                raise ValueError("내보내기 진행 count는 0 이상의 정수여야 합니다.")
            counts[_nonempty_text(key, "진행 count 이름")] = int(value)
        object.__setattr__(self, "job_id", _nonempty_text(self.job_id, "진행 job ID"))
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "fraction", fraction)
        object.__setattr__(self, "message", _nonempty_text(self.message, "진행 메시지"))
        object.__setattr__(self, "counts", MappingProxyType(counts))

    def to_mapping(self) -> dict[str, Any]:
        """progress.json에 쓸 JSON mapping을 반환합니다."""

        return _json_value(
            {
                "job_id": self.job_id,
                "stage": self.stage,
                "fraction": self.fraction,
                "message": self.message,
                "counts": self.counts,
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeExportProgress":
        """progress.json의 완성된 원자 객체만 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("진행률은 JSON 객체여야 합니다.")
        try:
            return cls(
                job_id=str(value["job_id"]),
                stage=str(value["stage"]),
                fraction=value["fraction"],
                message=str(value["message"]),
                counts=value.get("counts", {}),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise MergeProtocolError("진행률 JSON 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeExportResult:
    """worker 완료와 main atomic finalize 사이의 작은 결과 계약입니다."""

    job_id: str
    phase: MergePhase | str
    runtime_fingerprint: str
    candidate_root: Path | None
    output_root: Path
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[MergeIssue, ...] = ()
    error: MergeIssue | None = None

    def __post_init__(self) -> None:
        """성공·실패 결과의 job ID, phase, 경로와 count를 엄격히 검증합니다."""

        try:
            phase = MergePhase(self.phase)
        except ValueError as error:
            raise ValueError("내보내기 결과 phase가 올바르지 않습니다.") from error
        if not isinstance(self.counts, Mapping):
            raise ValueError("내보내기 결과 count는 mapping이어야 합니다.")
        counts: dict[str, int] = {}
        for key, value in self.counts.items():
            if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
                raise ValueError("내보내기 결과 count는 0 이상의 정수여야 합니다.")
            counts[_nonempty_text(key, "결과 count 이름")] = int(value)
        if not all(isinstance(item, MergeIssue) for item in self.warnings):
            raise ValueError("내보내기 경고 형식이 올바르지 않습니다.")
        if self.error is not None and not isinstance(self.error, MergeIssue):
            raise ValueError("내보내기 오류 형식이 올바르지 않습니다.")
        object.__setattr__(self, "job_id", _nonempty_text(self.job_id, "결과 job ID"))
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "runtime_fingerprint", _nonempty_text(self.runtime_fingerprint, "결과 Runtime fingerprint"))
        object.__setattr__(self, "candidate_root", Path(self.candidate_root) if self.candidate_root is not None else None)
        object.__setattr__(self, "output_root", Path(self.output_root))
        object.__setattr__(self, "counts", MappingProxyType(counts))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def is_success(self) -> bool:
        """후보 폴더를 최종 승격할 수 있는 worker 성공 상태인지 반환합니다."""

        return self.phase is MergePhase.WAITING_FOR_FINALIZE and self.error is None

    def to_mapping(self) -> dict[str, Any]:
        """result.json 또는 error.json에 쓸 JSON mapping을 반환합니다."""

        return _json_value(
            {
                "job_id": self.job_id,
                "phase": self.phase,
                "runtime_fingerprint": self.runtime_fingerprint,
                "candidate_root": self.candidate_root,
                "output_root": self.output_root,
                "counts": self.counts,
                "warnings": [item.to_mapping() for item in self.warnings],
                "error": self.error.to_mapping() if self.error is not None else None,
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeExportResult":
        """worker result/error JSON을 strict result 타입으로 복원합니다."""

        if not isinstance(value, Mapping):
            raise MergeProtocolError("내보내기 결과는 JSON 객체여야 합니다.")
        try:
            return cls(
                job_id=str(value["job_id"]),
                phase=str(value["phase"]),
                runtime_fingerprint=str(value["runtime_fingerprint"]),
                candidate_root=(Path(str(value["candidate_root"])) if value.get("candidate_root") is not None else None),
                output_root=Path(str(value["output_root"])),
                counts=value.get("counts", {}),
                warnings=tuple(MergeIssue.from_mapping(item) for item in value.get("warnings", ())),
                error=(MergeIssue.from_mapping(value["error"]) if value.get("error") is not None else None),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise MergeProtocolError("내보내기 결과 JSON 형식이 올바르지 않습니다.") from error


@dataclass(frozen=True, slots=True)
class MergeExportPollResult:
    """backend poll 한 번의 UI용 상태와 완료 결과를 함께 보관합니다."""

    phase: MergePhase | str
    progress: MergeExportProgress | None = None
    preflight: MergeExportPreflightResult | None = None
    result: MergeExportResult | None = None
    error: MergeIssue | None = None

    def __post_init__(self) -> None:
        """poll 응답의 phase와 하위 불변 타입을 검증합니다."""

        try:
            phase = MergePhase(self.phase)
        except ValueError as error:
            raise ValueError("poll phase가 올바르지 않습니다.") from error
        if self.progress is not None and not isinstance(self.progress, MergeExportProgress):
            raise ValueError("poll progress 형식이 올바르지 않습니다.")
        if self.preflight is not None and not isinstance(self.preflight, MergeExportPreflightResult):
            raise ValueError("poll preflight 형식이 올바르지 않습니다.")
        if self.result is not None and not isinstance(self.result, MergeExportResult):
            raise ValueError("poll result 형식이 올바르지 않습니다.")
        if self.error is not None and not isinstance(self.error, MergeIssue):
            raise ValueError("poll error 형식이 올바르지 않습니다.")
        object.__setattr__(self, "phase", phase)


__all__ = (
    "Float64Matrix",
    "MergeCancelledError",
    "MergeExportError",
    "MergeExportPollResult",
    "MergeExportPreflightResult",
    "MergeExportProgress",
    "MergeExportResult",
    "MergeExportSnapshot",
    "MergeIssue",
    "MergeOutputFormat",
    "MergePhase",
    "MergePolicy",
    "MergePreflightError",
    "MergeProtocolError",
    "MergeSourceModelSnapshot",
    "MergeStage",
    "MergeStaleResultError",
)
