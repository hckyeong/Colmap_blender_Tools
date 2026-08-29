"""ICP 수치 코어가 공유하는 불변 입력·설정·결과 타입을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from math import isfinite
from typing import Any, Mapping

import numpy as np

from ..constants import ICP_ALGORITHM_VERSION
from .types import Sim3Transform


def _freeze_float64_array(
    value: object,
    *,
    field_name: str,
    shape: tuple[int | None, ...],
) -> np.ndarray:
    """유한한 float64 배열을 복사해 읽기 전용 연속 배열로 고정합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.ndim != len(shape):
        raise ValueError(f"{field_name} 배열 차원이 올바르지 않습니다.")
    for actual_size, expected_size in zip(array.shape, shape, strict=True):
        if expected_size is not None and actual_size != expected_size:
            raise ValueError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    if not np.isfinite(array).all():
        raise ValueError(f"{field_name} 배열에 유한하지 않은 값이 있습니다.")
    array.setflags(write=False)
    return array


def _freeze_uint64_array(
    value: object,
    *,
    field_name: str,
    shape: tuple[int | None, ...],
) -> np.ndarray:
    """uint64 배열을 복사해 읽기 전용 연속 배열로 고정합니다."""

    source = np.asarray(value)
    if source.dtype.kind not in {"u", "i"}:
        raise ValueError(f"{field_name} 배열은 정수 dtype이어야 합니다.")
    if source.dtype.kind == "i" and np.any(source < 0):
        raise ValueError(f"{field_name} 배열에는 음수 값이 있을 수 없습니다.")
    array = np.array(source, dtype=np.uint64, order="C", copy=True)
    if array.ndim != len(shape):
        raise ValueError(f"{field_name} 배열 차원이 올바르지 않습니다.")
    for actual_size, expected_size in zip(array.shape, shape, strict=True):
        if expected_size is not None and actual_size != expected_size:
            raise ValueError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    array.setflags(write=False)
    return array


def _freeze_int64_array(
    value: object,
    *,
    field_name: str,
    shape: tuple[int | None, ...],
) -> np.ndarray:
    """int64 배열을 복사해 읽기 전용 연속 배열로 고정합니다."""

    source = np.asarray(value)
    if source.dtype.kind not in {"u", "i"}:
        raise ValueError(f"{field_name} 배열은 정수 dtype이어야 합니다.")
    array = np.array(source, dtype=np.int64, order="C", copy=True)
    if array.ndim != len(shape):
        raise ValueError(f"{field_name} 배열 차원이 올바르지 않습니다.")
    for actual_size, expected_size in zip(array.shape, shape, strict=True):
        if expected_size is not None and actual_size != expected_size:
            raise ValueError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    array.setflags(write=False)
    return array


def _freeze_matrix(value: object, field_name: str) -> np.ndarray:
    """직렬화 경계에서 쓸 유한한 4×4 행렬을 읽기 전용으로 고정합니다."""

    return _freeze_float64_array(value, field_name=field_name, shape=(4, 4))


def _canonical_fingerprint(value: object) -> str:
    """JSON으로 표현 가능한 설정값에서 결정적 짧은 지문을 생성합니다."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=20).hexdigest()


def _strict_nonnegative_int(value: object, field_name: str) -> int:
    """bool·문자열·실수 암묵 변환 없이 0 이상의 정수만 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise IcpInputError(f"{field_name}은 bool·문자열·실수가 아닌 정수여야 합니다.")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IcpInputError(f"{field_name} 정수 변환에 실패했습니다.") from error
    if normalized < 0:
        raise IcpInputError(f"{field_name}은 0 이상이어야 합니다.")
    return normalized


def _strict_positive_int(value: object, field_name: str) -> int:
    """공용 비음수 정수 검증 뒤 1 이상의 설정값만 허용합니다."""

    normalized = _strict_nonnegative_int(value, field_name)
    if normalized < 1:
        raise IcpInputError(f"{field_name}은 1 이상이어야 합니다.")
    return normalized


def _strict_finite_float(value: object, field_name: str) -> float:
    """bool·문자열 암묵 변환 없이 유한한 실수 설정을 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise IcpInputError(f"{field_name}은 bool·문자열이 아닌 실수여야 합니다.")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IcpInputError(f"{field_name} 실수 변환에 실패했습니다.") from error
    if not isfinite(normalized):
        raise IcpInputError(f"{field_name}은 유한해야 합니다.")
    return normalized


def _strict_exact_bool(value: object, field_name: str) -> bool:
    """worker JSON 경계에서 Python bool 리터럴만 명시적으로 허용합니다."""

    if type(value) is not bool:
        raise IcpInputError(f"{field_name}은 정확한 bool 값이어야 합니다.")
    return value


def _strict_mapping_sequence(value: object, field_name: str) -> tuple[object, ...]:
    """외부 JSON 배열이 문자열·사전으로 묵시적으로 순회되지 않도록 검증합니다."""

    if not isinstance(value, (list, tuple)):
        raise IcpInputError(f"{field_name}은 목록 또는 튜플이어야 합니다.")
    return tuple(value)


def _strict_revision_snapshot(value: object, field_name: str) -> tuple[tuple[str, int], ...]:
    """모델 revision 쌍의 정수 값을 외부 결과 경계에서 엄격히 복원합니다."""

    revisions: list[tuple[str, int]] = []
    for index, item in enumerate(_strict_mapping_sequence(value, field_name)):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise IcpInputError(f"{field_name}[{index}]은 (모델 ID, revision) 쌍이어야 합니다.")
        model_id = str(item[0]).strip()
        revision = _strict_nonnegative_int(item[1], f"{field_name}[{index}] revision")
        if not model_id:
            raise IcpInputError(f"{field_name}[{index}] 모델 ID가 비어 있습니다.")
        revisions.append((model_id, revision))
    return tuple(revisions)


def stream_array_fingerprint(array: np.ndarray, *, chunk_bytes: int = 1_048_576) -> str:
    """C-contiguous 배열을 고정 크기 chunk로 해시해 추가 대형 복사를 피합니다."""

    if not isinstance(array, np.ndarray) or not array.flags.c_contiguous:
        raise IcpInputError("스트리밍 배열 지문 입력은 C-contiguous ndarray여야 합니다.")
    normalized_chunk_bytes = _strict_positive_int(chunk_bytes, "스트리밍 배열 지문 chunk 크기")
    digest = hashlib.blake2b(digest_size=20)
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(repr(array.shape).encode("ascii"))
    bytes_view = memoryview(array).cast("B")
    for start in range(0, len(bytes_view), normalized_chunk_bytes):
        digest.update(bytes_view[start : start + normalized_chunk_bytes])
    return digest.hexdigest()


def _array_fingerprint(array: np.ndarray) -> str:
    """내부 입력 지문 계산을 스트리밍 public helper로 일관되게 수행합니다."""

    return stream_array_fingerprint(array)


class IcpError(RuntimeError):
    """ICP 수치 경계에서 호출자에게 전달하는 기본 오류입니다."""


class IcpInputError(IcpError):
    """ICP 입력 snapshot·설정·배열이 계약을 만족하지 않을 때 발생합니다."""


class IcpDegeneracyError(IcpError):
    """대응점 기하가 변환을 안정적으로 결정하지 못할 때 발생합니다."""


class IcpNormalCoverageError(IcpDegeneracyError):
    """유효 PCA 노멀 부족으로 point-to-plane 레벨을 계속할 수 없을 때 발생합니다."""


class IcpNumericalError(IcpError):
    """SVD·최근접점·변환 계산에서 비유한 결과가 나올 때 발생합니다."""


class IcpWorkerError(IcpError):
    """외부 worker 결과·IPC 계약이 올바르지 않을 때 사용하는 오류입니다."""


class IcpCancelledError(IcpError):
    """취소 probe가 계산 중단을 요청했을 때 발생합니다."""


class IcpStaleResultError(IcpError):
    """입력 snapshot과 결과 승인 시점이 달라졌을 때 사용하는 오류입니다."""


class IcpOverlapError(IcpError):
    """자동 AABB·점유 그리드에서 유효 겹침 후보를 찾지 못했을 때 발생합니다."""


class IcpQualityRejectedError(IcpError):
    """최종 품질이 미리보기 불가 REJECT일 때 이유와 품질을 보존합니다."""

    def __init__(self, message: str, quality: "IcpQualityMetrics | None" = None) -> None:
        """worker error 경계에서 품질과 사람이 읽을 이유를 함께 보관합니다."""

        super().__init__(message)
        self.quality = quality


class IcpPreset(str, Enum):
    """공개 UI와 worker manifest가 공유하는 계산 품질 프리셋입니다."""

    FAST = "FAST"
    BALANCED = "BALANCED"
    PRECISE = "PRECISE"


class IcpScaleMode(str, Enum):
    """마커 Sim(3) 스케일을 잠글지 제한 보정할지 나타냅니다."""

    LOCKED = "LOCKED"
    REFINE = "REFINE"


class IcpQualityStatus(str, Enum):
    """ICP 결과를 미리보기·검토·거부로 분류합니다."""

    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class IcpScaleRefineStatus(str, Enum):
    """제한된 스케일 보정의 명시적 실행 결과를 나타냅니다."""

    NOT_REQUESTED = "NOT_REQUESTED"
    SKIPPED = "SKIPPED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class IcpMovingCloudFrame(str, Enum):
    """moving NPY가 source local인지 초기 Project World인지 명시합니다."""

    SOURCE_LOCAL = "SOURCE_LOCAL"
    INITIAL_PROJECT_WORLD = "INITIAL_PROJECT_WORLD"


class IcpObjectiveKind(str, Enum):
    """해상도 레벨에서 사용하는 ICP 목적함수를 명시합니다."""

    POINT_TO_POINT = "POINT_TO_POINT"
    POINT_TO_PLANE = "POINT_TO_PLANE"


class IcpSolverStopReason(str, Enum):
    """수치적으로 유효한 ICP 레벨의 종료 사유를 구분합니다."""

    NONE = "NONE"
    CONVERGED = "CONVERGED"
    ITERATION_LIMIT = "ITERATION_LIMIT"
    OBJECTIVE_INCREASE = "OBJECTIVE_INCREASE"
    CORRESPONDENCE_OSCILLATION = "CORRESPONDENCE_OSCILLATION"
    NORMAL_COVERAGE_INSUFFICIENT = "NORMAL_COVERAGE_INSUFFICIENT"
    LEVEL_INLIER_DROP = "LEVEL_INLIER_DROP"
    RADIUS_MINIMUM_REACHED = "RADIUS_MINIMUM_REACHED"


class IcpRobustKernelKind(str, Enum):
    """Point-to-plane 선형계에 적용할 강건 가중치 종류를 나타냅니다."""

    HUBER = "HUBER"


@dataclass(frozen=True, slots=True)
class IcpPointCloud:
    """한 모델의 ICP 입력 등록점과 원본 안정 ID를 안전하게 보관합니다."""

    model_id: str
    positions_float64: np.ndarray
    point_ids_uint64: np.ndarray

    def __post_init__(self) -> None:
        """모델 ID·점 좌표·점 ID를 복사해 읽기 전용 입력으로 고정합니다."""

        model_id = str(self.model_id).strip()
        if not model_id:
            raise IcpInputError("ICP 점군 모델 ID가 비어 있습니다.")
        positions = _freeze_float64_array(
            self.positions_float64,
            field_name="ICP 점 좌표",
            shape=(None, 3),
        )
        point_ids = _freeze_uint64_array(
            self.point_ids_uint64,
            field_name="ICP 등록점 ID",
            shape=(positions.shape[0],),
        )
        if positions.shape[0] == 0:
            raise IcpInputError("ICP 점군은 비어 있을 수 없습니다.")
        if np.unique(point_ids).size != point_ids.size:
            raise IcpInputError("한 ICP 점군의 등록점 ID는 중복될 수 없습니다.")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "positions_float64", positions)
        object.__setattr__(self, "point_ids_uint64", point_ids)

    @classmethod
    def from_verified_arrays(
        cls,
        *,
        model_id: str,
        positions_float64: np.ndarray,
        point_ids_uint64: np.ndarray,
    ) -> "IcpPointCloud":
        """worker가 검증한 read-only C 배열·memmap을 복사 없이 제한적으로 감쌉니다.

        이 factory는 worker가 검증된 repository에서 chunk-write한 NPY에만 사용합니다.
        dtype·연속성·읽기 전용·유한값은 재검사하지만, 원본 크기의 중복 ID 배열을 만들지 않기 위해
        등록점 ID 전체 고유성은 repository 입력 계약에 맡깁니다. 호출자는 memmap 수명을 반환 객체보다
        길게 유지해야 합니다.
        """

        normalized_model_id = str(model_id).strip()
        if not normalized_model_id:
            raise IcpInputError("ICP 점군 모델 ID가 비어 있습니다.")
        if not isinstance(positions_float64, np.ndarray) or not isinstance(point_ids_uint64, np.ndarray):
            raise IcpInputError("검증된 ICP 점군은 ndarray 또는 memmap이어야 합니다.")
        if (
            positions_float64.dtype != np.dtype(np.float64)
            or positions_float64.ndim != 2
            or positions_float64.shape[1] != 3
            or not positions_float64.flags.c_contiguous
            or positions_float64.flags.writeable
            or not np.isfinite(positions_float64).all()
        ):
            raise IcpInputError("검증된 ICP 좌표는 읽기 전용 C-contiguous float64 N×3이어야 합니다.")
        if (
            point_ids_uint64.dtype != np.dtype(np.uint64)
            or point_ids_uint64.ndim != 1
            or point_ids_uint64.shape[0] != positions_float64.shape[0]
            or not point_ids_uint64.flags.c_contiguous
            or point_ids_uint64.flags.writeable
        ):
            raise IcpInputError("검증된 ICP 등록점 ID는 읽기 전용 C-contiguous uint64여야 합니다.")
        if positions_float64.shape[0] == 0:
            raise IcpInputError("검증된 ICP 점군의 점 수가 올바르지 않습니다.")
        instance = object.__new__(cls)
        object.__setattr__(instance, "model_id", normalized_model_id)
        object.__setattr__(instance, "positions_float64", positions_float64)
        object.__setattr__(instance, "point_ids_uint64", point_ids_uint64)
        return instance

    @property
    def count(self) -> int:
        """원본 ICP 입력 점 개수를 반환합니다."""

        return int(self.positions_float64.shape[0])

    @property
    def fingerprint(self) -> str:
        """원본 좌표와 등록점 ID를 포함한 결정적 점군 지문을 반환합니다."""

        return _canonical_fingerprint(
            {
                "model_id": self.model_id,
                "positions": _array_fingerprint(self.positions_float64),
                "point_ids": _array_fingerprint(self.point_ids_uint64),
            }
        )


@dataclass(frozen=True, slots=True)
class IcpLevelParameters:
    """한 coarse·medium·fine 해상도 레벨의 예산과 반복 조건입니다."""

    name: str
    moving_budget: int
    reference_budget: int
    max_iterations: int
    trim_fraction: float
    convergence_required: int = 3
    objective_kind: IcpObjectiveKind | str | None = None
    normal_radius_multiplier: float = 3.0
    normal_minimum_neighbors: int = 16
    normal_maximum_neighbors: int = 28
    normal_minimum_planarity: float = 0.05
    normal_maximum_curvature: float = 0.25
    normal_maximum_angle_degrees: float = 55.0
    radius_shrink_factor: float = 0.80
    radius_minimum_voxel_multiplier: float = 2.0
    evaluation_budget: int = 4_096
    macro_cell_divisions: int = 4

    def __post_init__(self) -> None:
        """수치 solver가 사용할 레벨 설정 범위를 검증합니다."""

        name = str(self.name).strip().lower()
        if not name:
            raise IcpInputError("ICP 레벨 이름이 비어 있습니다.")
        moving_budget = _strict_positive_int(self.moving_budget, "ICP 이동 점 예산")
        reference_budget = _strict_positive_int(self.reference_budget, "ICP 기준 점 예산")
        max_iterations = _strict_positive_int(self.max_iterations, "ICP 최대 반복")
        convergence_required = _strict_positive_int(self.convergence_required, "ICP 수렴 반복")
        trim_fraction = _strict_finite_float(self.trim_fraction, "ICP trim 비율")
        try:
            objective_kind = (
                IcpObjectiveKind.POINT_TO_POINT
                if self.objective_kind is None and name == "coarse"
                else IcpObjectiveKind.POINT_TO_PLANE
                if self.objective_kind is None
                else self.objective_kind
                if isinstance(self.objective_kind, IcpObjectiveKind)
                else IcpObjectiveKind(str(self.objective_kind))
            )
        except ValueError as error:
            raise IcpInputError("ICP 레벨 목적함수가 올바르지 않습니다.") from error
        normal_radius_multiplier = _strict_finite_float(
            self.normal_radius_multiplier,
            "ICP 노멀 반경 배수",
        )
        normal_minimum_neighbors = _strict_positive_int(
            self.normal_minimum_neighbors,
            "ICP 노멀 최소 이웃 수",
        )
        normal_maximum_neighbors = _strict_positive_int(
            self.normal_maximum_neighbors,
            "ICP 노멀 최대 이웃 수",
        )
        normal_minimum_planarity = _strict_finite_float(
            self.normal_minimum_planarity,
            "ICP 노멀 최소 평면성",
        )
        normal_maximum_curvature = _strict_finite_float(
            self.normal_maximum_curvature,
            "ICP 노멀 최대 곡률",
        )
        normal_maximum_angle_degrees = _strict_finite_float(
            self.normal_maximum_angle_degrees,
            "ICP 노멀 최대 각도",
        )
        radius_shrink_factor = _strict_finite_float(
            self.radius_shrink_factor,
            "ICP 반경 축소 계수",
        )
        radius_minimum_voxel_multiplier = _strict_finite_float(
            self.radius_minimum_voxel_multiplier,
            "ICP 최소 반경 voxel 배수",
        )
        evaluation_budget = _strict_positive_int(self.evaluation_budget, "ICP 고정 평가 예산")
        macro_cell_divisions = _strict_positive_int(self.macro_cell_divisions, "ICP macro-cell 분할 수")
        if moving_budget < 3 or reference_budget < 3:
            raise IcpInputError("ICP 레벨 점 예산은 각각 3 이상이어야 합니다.")
        if not isfinite(trim_fraction) or not 0.0 < trim_fraction <= 1.0:
            raise IcpInputError("ICP trim 비율은 0보다 크고 1 이하여야 합니다.")
        if normal_radius_multiplier <= 0.0 or radius_minimum_voxel_multiplier <= 0.0:
            raise IcpInputError("ICP 노멀·최소 반경 배수는 양수여야 합니다.")
        if normal_minimum_neighbors < 3 or normal_maximum_neighbors < normal_minimum_neighbors:
            raise IcpInputError("ICP 노멀 이웃 수 범위가 올바르지 않습니다.")
        if not 0.0 <= normal_minimum_planarity <= 1.0 or not 0.0 <= normal_maximum_curvature <= 1.0:
            raise IcpInputError("ICP 노멀 평면성·곡률 범위가 올바르지 않습니다.")
        if not 0.0 < normal_maximum_angle_degrees <= 90.0:
            raise IcpInputError("ICP 노멀 최대 각도는 0보다 크고 90도 이하여야 합니다.")
        if not 0.0 < radius_shrink_factor <= 1.0:
            raise IcpInputError("ICP 반경 축소 계수는 0보다 크고 1 이하여야 합니다.")
        if evaluation_budget < 3:
            raise IcpInputError("ICP 고정 평가 예산은 3 이상이어야 합니다.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "moving_budget", moving_budget)
        object.__setattr__(self, "reference_budget", reference_budget)
        object.__setattr__(self, "max_iterations", max_iterations)
        object.__setattr__(self, "trim_fraction", trim_fraction)
        object.__setattr__(self, "convergence_required", convergence_required)
        object.__setattr__(self, "objective_kind", objective_kind)
        object.__setattr__(self, "normal_radius_multiplier", normal_radius_multiplier)
        object.__setattr__(self, "normal_minimum_neighbors", normal_minimum_neighbors)
        object.__setattr__(self, "normal_maximum_neighbors", normal_maximum_neighbors)
        object.__setattr__(self, "normal_minimum_planarity", normal_minimum_planarity)
        object.__setattr__(self, "normal_maximum_curvature", normal_maximum_curvature)
        object.__setattr__(self, "normal_maximum_angle_degrees", normal_maximum_angle_degrees)
        object.__setattr__(self, "radius_shrink_factor", radius_shrink_factor)
        object.__setattr__(self, "radius_minimum_voxel_multiplier", radius_minimum_voxel_multiplier)
        object.__setattr__(self, "evaluation_budget", evaluation_budget)
        object.__setattr__(self, "macro_cell_divisions", macro_cell_divisions)

    def to_mapping(self) -> dict[str, object]:
        """worker manifest에 안전하게 쓸 레벨 설정 사전을 반환합니다."""

        return {
            "name": self.name,
            "moving_budget": self.moving_budget,
            "reference_budget": self.reference_budget,
            "max_iterations": self.max_iterations,
            "trim_fraction": self.trim_fraction,
            "convergence_required": self.convergence_required,
            "objective_kind": self.objective_kind.value,
            "normal_radius_multiplier": self.normal_radius_multiplier,
            "normal_minimum_neighbors": self.normal_minimum_neighbors,
            "normal_maximum_neighbors": self.normal_maximum_neighbors,
            "normal_minimum_planarity": self.normal_minimum_planarity,
            "normal_maximum_curvature": self.normal_maximum_curvature,
            "normal_maximum_angle_degrees": self.normal_maximum_angle_degrees,
            "radius_shrink_factor": self.radius_shrink_factor,
            "radius_minimum_voxel_multiplier": self.radius_minimum_voxel_multiplier,
            "evaluation_budget": self.evaluation_budget,
            "macro_cell_divisions": self.macro_cell_divisions,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpLevelParameters":
        """worker manifest의 레벨 설정 사전에서 불변 설정을 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpInputError("ICP 레벨 설정은 사전이어야 합니다.")
        allowed_fields = {
            "name",
            "moving_budget",
            "reference_budget",
            "max_iterations",
            "trim_fraction",
            "convergence_required",
            "objective_kind",
            "normal_radius_multiplier",
            "normal_minimum_neighbors",
            "normal_maximum_neighbors",
            "normal_minimum_planarity",
            "normal_maximum_curvature",
            "normal_maximum_angle_degrees",
            "radius_shrink_factor",
            "radius_minimum_voxel_multiplier",
            "evaluation_budget",
            "macro_cell_divisions",
        }
        unsupported = set(value).difference(allowed_fields)
        if unsupported:
            joined = ", ".join(sorted(str(item) for item in unsupported))
            raise IcpInputError(f"지원하지 않는 ICP 레벨 설정 필드가 있습니다: {joined}")
        try:
            return cls(
                name=str(value["name"]),
                moving_budget=_strict_positive_int(value["moving_budget"], "ICP 이동 점 예산"),
                reference_budget=_strict_positive_int(value["reference_budget"], "ICP 기준 점 예산"),
                max_iterations=_strict_positive_int(value["max_iterations"], "ICP 최대 반복"),
                trim_fraction=_strict_finite_float(value["trim_fraction"], "ICP trim 비율"),
                convergence_required=_strict_positive_int(
                    value.get("convergence_required", 3),
                    "ICP 수렴 반복",
                ),
                objective_kind=(
                    IcpObjectiveKind(str(value["objective_kind"]))
                    if "objective_kind" in value
                    else None
                ),
                normal_radius_multiplier=_strict_finite_float(
                    value.get("normal_radius_multiplier", 3.0),
                    "ICP 노멀 반경 배수",
                ),
                normal_minimum_neighbors=_strict_positive_int(
                    value.get("normal_minimum_neighbors", 16),
                    "ICP 노멀 최소 이웃 수",
                ),
                normal_maximum_neighbors=_strict_positive_int(
                    value.get("normal_maximum_neighbors", 28),
                    "ICP 노멀 최대 이웃 수",
                ),
                normal_minimum_planarity=_strict_finite_float(
                    value.get("normal_minimum_planarity", 0.05),
                    "ICP 노멀 최소 평면성",
                ),
                normal_maximum_curvature=_strict_finite_float(
                    value.get("normal_maximum_curvature", 0.25),
                    "ICP 노멀 최대 곡률",
                ),
                normal_maximum_angle_degrees=_strict_finite_float(
                    value.get("normal_maximum_angle_degrees", 55.0),
                    "ICP 노멀 최대 각도",
                ),
                radius_shrink_factor=_strict_finite_float(
                    value.get("radius_shrink_factor", 0.80),
                    "ICP 반경 축소 계수",
                ),
                radius_minimum_voxel_multiplier=_strict_finite_float(
                    value.get("radius_minimum_voxel_multiplier", 2.0),
                    "ICP 최소 반경 voxel 배수",
                ),
                evaluation_budget=_strict_positive_int(
                    value.get("evaluation_budget", 4_096),
                    "ICP 고정 평가 예산",
                ),
                macro_cell_divisions=_strict_positive_int(
                    value.get("macro_cell_divisions", 4),
                    "ICP macro-cell 분할 수",
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise IcpInputError("ICP 레벨 설정 복원에 실패했습니다.") from error


def _preset_levels(preset: IcpPreset) -> tuple[IcpLevelParameters, ...]:
    """계획서에 고정한 프리셋별 다중 해상도 기본값을 반환합니다."""

    if preset is IcpPreset.FAST:
        return (
            IcpLevelParameters("coarse", 20_000, 40_000, 12, 0.70, 2),
            IcpLevelParameters("fine", 60_000, 120_000, 8, 0.70, 2),
        )
    if preset is IcpPreset.PRECISE:
        return (
            IcpLevelParameters("coarse", 50_000, 100_000, 20, 0.85, 3),
            IcpLevelParameters("medium", 150_000, 300_000, 15, 0.85, 3),
            IcpLevelParameters("fine", 250_000, 500_000, 10, 0.85, 3),
        )
    return (
        IcpLevelParameters("coarse", 25_000, 50_000, 15, 0.80, 3),
        IcpLevelParameters("medium", 75_000, 150_000, 12, 0.80, 3),
        IcpLevelParameters("fine", 150_000, 250_000, 8, 0.80, 3),
    )


@dataclass(frozen=True, slots=True)
class IcpParameters:
    """ICP worker와 UI가 공유하는 검증된 전체 수치 설정입니다."""

    preset: IcpPreset = IcpPreset.BALANCED
    scale_mode: IcpScaleMode = IcpScaleMode.LOCKED
    levels: tuple[IcpLevelParameters, ...] = ()
    minimum_correspondences: int = 30
    overlap_minimum_common_cells: int = 32
    overlap_minimum_moving_coverage: float = 0.01
    mad_multiplier: float = 3.0
    query_chunk_size: int = 2_048
    scale_refine_max_relative_change: float = 0.05
    scale_refine_max_step: float = 0.005
    scale_refine_max_iterations: int = 5
    scale_refine_min_rmse_improvement: float = 0.02
    scale_refine_min_inliers: int = 100
    scale_refine_min_fitness: float = 0.30
    scale_refine_min_reciprocal: float = 0.40
    scale_refine_min_overlap_coverage: float = 0.30
    max_rotation_degrees: float = 30.0
    max_translation_fraction: float = 0.25
    best_candidate_enabled: bool = True
    no_improvement_policy: IcpQualityStatus | str = IcpQualityStatus.REVIEW
    normal_angle_uses_absolute_dot: bool = True
    normal_condition_limit: float = 1.0e10
    point_to_plane_damping: float = 1.0e-6
    robust_kernel_kind: IcpRobustKernelKind | str = IcpRobustKernelKind.HUBER
    robust_kernel_scale: float = 1.345

    def __post_init__(self) -> None:
        """프리셋·scale refine·필터 설정을 범위 안의 불변 값으로 고정합니다."""

        try:
            preset = self.preset if isinstance(self.preset, IcpPreset) else IcpPreset(str(self.preset))
            scale_mode = (
                self.scale_mode
                if isinstance(self.scale_mode, IcpScaleMode)
                else IcpScaleMode(str(self.scale_mode))
            )
        except ValueError as error:
            raise IcpInputError("지원하지 않는 ICP 프리셋 또는 스케일 모드입니다.") from error
        try:
            no_improvement_policy = (
                self.no_improvement_policy
                if isinstance(self.no_improvement_policy, IcpQualityStatus)
                else IcpQualityStatus(str(self.no_improvement_policy))
            )
            robust_kernel_kind = (
                self.robust_kernel_kind
                if isinstance(self.robust_kernel_kind, IcpRobustKernelKind)
                else IcpRobustKernelKind(str(self.robust_kernel_kind))
            )
        except ValueError as error:
            raise IcpInputError("ICP 검토 정책 또는 강건 커널이 올바르지 않습니다.") from error
        if no_improvement_policy is not IcpQualityStatus.REVIEW:
            raise IcpInputError("ICP 개선 부족 정책은 REVIEW여야 합니다.")
        raw_levels = tuple(self.levels) if self.levels else _preset_levels(preset)
        if not raw_levels:
            raise IcpInputError("ICP에는 하나 이상의 해상도 레벨이 필요합니다.")
        levels = tuple(
            level if isinstance(level, IcpLevelParameters) else IcpLevelParameters.from_mapping(level)
            for level in raw_levels
        )
        names = tuple(level.name for level in levels)
        if len(set(names)) != len(names):
            raise IcpInputError("ICP 해상도 레벨 이름은 중복될 수 없습니다.")
        integer_fields = {
            "minimum_correspondences": _strict_positive_int(self.minimum_correspondences, "최소 대응점 수"),
            "overlap_minimum_common_cells": _strict_positive_int(self.overlap_minimum_common_cells, "최소 overlap cell 수"),
            "query_chunk_size": _strict_positive_int(self.query_chunk_size, "NN query chunk 크기"),
            "scale_refine_max_iterations": _strict_positive_int(self.scale_refine_max_iterations, "스케일 보정 반복"),
            "scale_refine_min_inliers": _strict_positive_int(self.scale_refine_min_inliers, "스케일 보정 최소 inlier"),
        }
        float_fields = {
            "overlap_minimum_moving_coverage": _strict_finite_float(self.overlap_minimum_moving_coverage, "최소 overlap coverage"),
            "mad_multiplier": _strict_finite_float(self.mad_multiplier, "ICP MAD 배수"),
            "scale_refine_max_relative_change": _strict_finite_float(self.scale_refine_max_relative_change, "스케일 보정 전체 한도"),
            "scale_refine_max_step": _strict_finite_float(self.scale_refine_max_step, "스케일 보정 1회 한도"),
            "scale_refine_min_rmse_improvement": _strict_finite_float(self.scale_refine_min_rmse_improvement, "스케일 보정 최소 RMSE 개선율"),
            "scale_refine_min_fitness": _strict_finite_float(self.scale_refine_min_fitness, "스케일 보정 최소 fitness"),
            "scale_refine_min_reciprocal": _strict_finite_float(self.scale_refine_min_reciprocal, "스케일 보정 최소 reciprocal"),
            "scale_refine_min_overlap_coverage": _strict_finite_float(self.scale_refine_min_overlap_coverage, "스케일 보정 최소 overlap coverage"),
            "max_rotation_degrees": _strict_finite_float(self.max_rotation_degrees, "ICP 안전 회전 한도"),
            "max_translation_fraction": _strict_finite_float(self.max_translation_fraction, "ICP 안전 이동 한도"),
            "normal_condition_limit": _strict_finite_float(self.normal_condition_limit, "ICP 노멀 선형계 condition 한도"),
            "point_to_plane_damping": _strict_finite_float(self.point_to_plane_damping, "ICP point-to-plane damping"),
            "robust_kernel_scale": _strict_finite_float(self.robust_kernel_scale, "ICP 강건 커널 배수"),
        }
        if not all(isfinite(value) for value in float_fields.values()):
            raise IcpInputError("ICP 실수 설정은 모두 유한해야 합니다.")
        ratio_fields = (
            "overlap_minimum_moving_coverage",
            "scale_refine_max_relative_change",
            "scale_refine_max_step",
            "scale_refine_min_rmse_improvement",
            "scale_refine_min_fitness",
            "scale_refine_min_reciprocal",
            "scale_refine_min_overlap_coverage",
        )
        if any(not 0.0 <= float_fields[name] <= 1.0 for name in ratio_fields):
            raise IcpInputError("ICP 비율 설정은 0 이상 1 이하여야 합니다.")
        if float_fields["mad_multiplier"] <= 0.0:
            raise IcpInputError("ICP MAD 배수는 양수여야 합니다.")
        if float_fields["max_rotation_degrees"] <= 0.0 or float_fields["max_translation_fraction"] <= 0.0:
            raise IcpInputError("ICP 안전 이동·회전 한도는 양수여야 합니다.")
        if (
            float_fields["normal_condition_limit"] <= 1.0
            or float_fields["point_to_plane_damping"] < 0.0
            or float_fields["robust_kernel_scale"] <= 0.0
        ):
            raise IcpInputError("ICP 노멀 선형계·강건 커널 설정이 올바르지 않습니다.")
        if float_fields["scale_refine_max_step"] > float_fields["scale_refine_max_relative_change"]:
            raise IcpInputError("스케일 보정 1회 한도는 전체 한도보다 클 수 없습니다.")
        object.__setattr__(self, "preset", preset)
        object.__setattr__(self, "scale_mode", scale_mode)
        object.__setattr__(self, "no_improvement_policy", no_improvement_policy)
        object.__setattr__(self, "robust_kernel_kind", robust_kernel_kind)
        best_candidate_enabled = _strict_exact_bool(
            self.best_candidate_enabled,
            "ICP 최선 후보 보존 여부",
        )
        if not best_candidate_enabled:
            raise IcpInputError("ICP 최선 후보 보존은 항상 활성화되어야 합니다.")
        object.__setattr__(self, "best_candidate_enabled", True)
        object.__setattr__(
            self,
            "normal_angle_uses_absolute_dot",
            _strict_exact_bool(self.normal_angle_uses_absolute_dot, "ICP 노멀 절대 dot 사용 여부"),
        )
        object.__setattr__(self, "levels", levels)
        for name, value in integer_fields.items():
            object.__setattr__(self, name, value)
        for name, value in float_fields.items():
            object.__setattr__(self, name, value)

    @property
    def scale_refine_enabled(self) -> bool:
        """UI·manifest 호환을 위한 scale refine 활성 여부를 반환합니다."""

        return self.scale_mode is IcpScaleMode.REFINE

    @property
    def fingerprint(self) -> str:
        """현재 설정의 worker 재현성 검사용 결정적 지문을 반환합니다."""

        return _canonical_fingerprint(self.to_mapping())

    @classmethod
    def for_preset(
        cls,
        preset: IcpPreset | str = IcpPreset.BALANCED,
        *,
        scale_refine_enabled: bool = False,
    ) -> "IcpParameters":
        """UI가 전달한 프리셋과 보정 토글로 기본 ICP 설정을 생성합니다."""

        normalized = preset if isinstance(preset, IcpPreset) else IcpPreset(str(preset))
        return cls(
            preset=normalized,
            scale_mode=IcpScaleMode.REFINE if scale_refine_enabled else IcpScaleMode.LOCKED,
        )

    def to_mapping(self) -> dict[str, object]:
        """JSON manifest에 저장할 전체 설정을 원시값 사전으로 반환합니다."""

        return {
            "preset": self.preset.value,
            "scale_mode": self.scale_mode.value,
            "scale_refine_enabled": self.scale_refine_enabled,
            "levels": [level.to_mapping() for level in self.levels],
            "minimum_correspondences": self.minimum_correspondences,
            "overlap_minimum_common_cells": self.overlap_minimum_common_cells,
            "overlap_minimum_moving_coverage": self.overlap_minimum_moving_coverage,
            "mad_multiplier": self.mad_multiplier,
            "query_chunk_size": self.query_chunk_size,
            "scale_refine_max_relative_change": self.scale_refine_max_relative_change,
            "scale_refine_max_step": self.scale_refine_max_step,
            "scale_refine_max_iterations": self.scale_refine_max_iterations,
            "scale_refine_min_rmse_improvement": self.scale_refine_min_rmse_improvement,
            "scale_refine_min_inliers": self.scale_refine_min_inliers,
            "scale_refine_min_fitness": self.scale_refine_min_fitness,
            "scale_refine_min_reciprocal": self.scale_refine_min_reciprocal,
            "scale_refine_min_overlap_coverage": self.scale_refine_min_overlap_coverage,
            "max_rotation_degrees": self.max_rotation_degrees,
            "max_translation_fraction": self.max_translation_fraction,
            "best_candidate_enabled": self.best_candidate_enabled,
            "no_improvement_policy": self.no_improvement_policy.value,
            "normal_angle_uses_absolute_dot": self.normal_angle_uses_absolute_dot,
            "normal_condition_limit": self.normal_condition_limit,
            "point_to_plane_damping": self.point_to_plane_damping,
            "robust_kernel_kind": self.robust_kernel_kind.value,
            "robust_kernel_scale": self.robust_kernel_scale,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpParameters":
        """검증된 worker manifest 사전에서 설정을 손실 없이 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpInputError("ICP 설정은 사전이어야 합니다.")
        allowed_fields = {
            "preset",
            "scale_mode",
            "scale_refine_enabled",
            "levels",
            "minimum_correspondences",
            "overlap_minimum_common_cells",
            "overlap_minimum_moving_coverage",
            "mad_multiplier",
            "query_chunk_size",
            "scale_refine_max_relative_change",
            "scale_refine_max_step",
            "scale_refine_max_iterations",
            "scale_refine_min_rmse_improvement",
            "scale_refine_min_inliers",
            "scale_refine_min_fitness",
            "scale_refine_min_reciprocal",
            "scale_refine_min_overlap_coverage",
            "max_rotation_degrees",
            "max_translation_fraction",
            "best_candidate_enabled",
            "no_improvement_policy",
            "normal_angle_uses_absolute_dot",
            "normal_condition_limit",
            "point_to_plane_damping",
            "robust_kernel_kind",
            "robust_kernel_scale",
        }
        unsupported = set(value).difference(allowed_fields)
        if unsupported:
            joined = ", ".join(sorted(str(item) for item in unsupported))
            raise IcpInputError(f"지원하지 않는 ICP 설정 필드가 있습니다: {joined}")
        try:
            preset = IcpPreset(str(value.get("preset", IcpPreset.BALANCED.value)))
            if "scale_mode" in value:
                scale_mode = IcpScaleMode(str(value["scale_mode"]))
                if "scale_refine_enabled" in value:
                    enabled = _strict_exact_bool(value["scale_refine_enabled"], "ICP scale_refine_enabled")
                    if enabled != (scale_mode is IcpScaleMode.REFINE):
                        raise IcpInputError("ICP scale_mode와 scale_refine_enabled가 일치하지 않습니다.")
            else:
                enabled = (
                    _strict_exact_bool(value["scale_refine_enabled"], "ICP scale_refine_enabled")
                    if "scale_refine_enabled" in value
                    else False
                )
                scale_mode = (
                    IcpScaleMode.REFINE
                    if enabled
                    else IcpScaleMode.LOCKED
                )
            raw_levels = _strict_mapping_sequence(value.get("levels", ()), "ICP 해상도 레벨")
            levels = tuple(IcpLevelParameters.from_mapping(item) for item in raw_levels)
            return cls(
                preset=preset,
                scale_mode=scale_mode,
                levels=levels,
                minimum_correspondences=_strict_positive_int(
                    value.get("minimum_correspondences", 30),
                    "최소 대응점 수",
                ),
                overlap_minimum_common_cells=_strict_positive_int(
                    value.get("overlap_minimum_common_cells", 32),
                    "최소 overlap cell 수",
                ),
                overlap_minimum_moving_coverage=_strict_finite_float(value.get("overlap_minimum_moving_coverage", 0.01), "최소 overlap coverage"),
                mad_multiplier=_strict_finite_float(value.get("mad_multiplier", 3.0), "ICP MAD 배수"),
                query_chunk_size=_strict_positive_int(
                    value.get("query_chunk_size", 2_048),
                    "NN query chunk 크기",
                ),
                scale_refine_max_relative_change=_strict_finite_float(value.get("scale_refine_max_relative_change", 0.05), "스케일 보정 전체 한도"),
                scale_refine_max_step=_strict_finite_float(value.get("scale_refine_max_step", 0.005), "스케일 보정 1회 한도"),
                scale_refine_max_iterations=_strict_positive_int(
                    value.get("scale_refine_max_iterations", 5),
                    "스케일 보정 반복",
                ),
                scale_refine_min_rmse_improvement=_strict_finite_float(value.get("scale_refine_min_rmse_improvement", 0.02), "스케일 보정 최소 RMSE 개선율"),
                scale_refine_min_inliers=_strict_positive_int(
                    value.get("scale_refine_min_inliers", 100),
                    "스케일 보정 최소 inlier",
                ),
                scale_refine_min_fitness=_strict_finite_float(value.get("scale_refine_min_fitness", 0.30), "스케일 보정 최소 fitness"),
                scale_refine_min_reciprocal=_strict_finite_float(value.get("scale_refine_min_reciprocal", 0.40), "스케일 보정 최소 reciprocal"),
                scale_refine_min_overlap_coverage=_strict_finite_float(value.get("scale_refine_min_overlap_coverage", 0.30), "스케일 보정 최소 overlap coverage"),
                max_rotation_degrees=_strict_finite_float(value.get("max_rotation_degrees", 30.0), "ICP 안전 회전 한도"),
                max_translation_fraction=_strict_finite_float(value.get("max_translation_fraction", 0.25), "ICP 안전 이동 한도"),
                best_candidate_enabled=_strict_exact_bool(
                    value.get("best_candidate_enabled", True),
                    "ICP 최선 후보 보존 여부",
                ),
                no_improvement_policy=IcpQualityStatus(
                    str(value.get("no_improvement_policy", IcpQualityStatus.REVIEW.value))
                ),
                normal_angle_uses_absolute_dot=_strict_exact_bool(
                    value.get("normal_angle_uses_absolute_dot", True),
                    "ICP 노멀 절대 dot 사용 여부",
                ),
                normal_condition_limit=_strict_finite_float(
                    value.get("normal_condition_limit", 1.0e10),
                    "ICP 노멀 선형계 condition 한도",
                ),
                point_to_plane_damping=_strict_finite_float(
                    value.get("point_to_plane_damping", 1.0e-6),
                    "ICP point-to-plane damping",
                ),
                robust_kernel_kind=IcpRobustKernelKind(
                    str(value.get("robust_kernel_kind", IcpRobustKernelKind.HUBER.value))
                ),
                robust_kernel_scale=_strict_finite_float(
                    value.get("robust_kernel_scale", 1.345),
                    "ICP 강건 커널 배수",
                ),
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise IcpInputError("ICP 설정 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpInputSnapshot:
    """worker가 원본을 수정하지 않고 계산할 완전한 ICP 입력 snapshot입니다."""

    moving_model_id: str
    reference_model_ids: tuple[str, ...]
    moving_cloud: IcpPointCloud
    reference_clouds: tuple[IcpPointCloud, ...]
    initial_alignment: Sim3Transform
    marker_uniform_scale: float
    parameters: IcpParameters
    marker_max_error: float = 0.0
    marker_rms: float = 0.0
    marker_reference_project: np.ndarray | None = None
    marker_moving_project: np.ndarray | None = None
    marker_signature: str = ""
    model_revision_snapshot: tuple[tuple[str, int], ...] = ()
    input_fingerprint: str = ""
    algorithm_version: str = ICP_ALGORITHM_VERSION
    moving_cloud_frame: IcpMovingCloudFrame = IcpMovingCloudFrame.SOURCE_LOCAL

    def __post_init__(self) -> None:
        """모델·마커·정렬·점군 snapshot의 일관성과 읽기 전용 상태를 검증합니다."""

        moving_model_id = str(self.moving_model_id).strip()
        if not moving_model_id:
            raise IcpInputError("ICP 이동 모델 ID가 비어 있습니다.")
        if not isinstance(self.moving_cloud, IcpPointCloud):
            raise IcpInputError("ICP 이동 점군 형식이 올바르지 않습니다.")
        if self.moving_cloud.model_id != moving_model_id:
            raise IcpInputError("ICP 이동 모델 ID와 점군 소유자가 다릅니다.")
        clouds = tuple(self.reference_clouds)
        if not clouds or not all(isinstance(cloud, IcpPointCloud) for cloud in clouds):
            raise IcpInputError("ICP 기준 점군이 하나 이상 필요합니다.")
        reference_model_ids = tuple(str(model_id).strip() for model_id in self.reference_model_ids)
        if not reference_model_ids or any(not model_id for model_id in reference_model_ids):
            raise IcpInputError("ICP 기준 모델 ID가 비어 있습니다.")
        if len(set(reference_model_ids)) != len(reference_model_ids):
            raise IcpInputError("ICP 기준 모델 ID는 중복될 수 없습니다.")
        if tuple(cloud.model_id for cloud in clouds) != reference_model_ids:
            raise IcpInputError("ICP 기준 모델 ID 순서와 점군 순서가 다릅니다.")
        if moving_model_id in reference_model_ids:
            raise IcpInputError("ICP 이동 모델은 기준 모델에 포함될 수 없습니다.")
        if not isinstance(self.initial_alignment, Sim3Transform):
            raise IcpInputError("ICP 초기 정렬은 Sim3Transform이어야 합니다.")
        marker_uniform_scale = _strict_finite_float(self.marker_uniform_scale, "마커 균일 스케일")
        marker_max_error = _strict_finite_float(self.marker_max_error, "마커 최대 오차")
        marker_rms = _strict_finite_float(self.marker_rms, "마커 RMS")
        if not isfinite(marker_uniform_scale) or marker_uniform_scale <= 0.0:
            raise IcpInputError("마커 균일 스케일은 유한한 양수여야 합니다.")
        if not isfinite(marker_max_error) or marker_max_error < 0.0:
            raise IcpInputError("마커 최대 오차는 유한한 0 이상이어야 합니다.")
        if not isfinite(marker_rms) or marker_rms < 0.0:
            raise IcpInputError("마커 RMS는 유한한 0 이상이어야 합니다.")
        if not np.isclose(
            self.initial_alignment.uniform_scale,
            marker_uniform_scale,
            rtol=0.0,
            atol=1.0e-9 * max(1.0, marker_uniform_scale),
        ):
            raise IcpInputError("ICP 초기 정렬 스케일과 marker 스케일이 다릅니다.")
        parameters = self.parameters
        if not isinstance(parameters, IcpParameters):
            raise IcpInputError("ICP parameters 형식이 올바르지 않습니다.")
        try:
            moving_cloud_frame = (
                self.moving_cloud_frame
                if isinstance(self.moving_cloud_frame, IcpMovingCloudFrame)
                else IcpMovingCloudFrame(str(self.moving_cloud_frame))
            )
        except ValueError as error:
            raise IcpInputError("ICP moving cloud 좌표계가 올바르지 않습니다.") from error
        reference_markers = self.marker_reference_project
        moving_markers = self.marker_moving_project
        if (reference_markers is None) != (moving_markers is None):
            raise IcpInputError("마커 drift 계산에는 A/P 마커 좌표가 함께 필요합니다.")
        if reference_markers is not None and moving_markers is not None:
            reference_markers = _freeze_float64_array(
                reference_markers,
                field_name="ICP A 마커",
                shape=(None, 3),
            )
            moving_markers = _freeze_float64_array(
                moving_markers,
                field_name="ICP P 마커",
                shape=(reference_markers.shape[0], 3),
            )
            if reference_markers.shape[0] < 3:
                raise IcpInputError("ICP marker drift 입력에는 마커 3개 이상이 필요합니다.")
        marker_signature = str(self.marker_signature).strip()
        revisions = tuple(sorted(_strict_revision_snapshot(self.model_revision_snapshot, "ICP 모델 revision snapshot")))
        if not marker_signature:
            raise IcpInputError("ICP marker signature가 비어 있습니다.")
        if not revisions or any(not model_id or revision < 0 for model_id, revision in revisions):
            raise IcpInputError("ICP 모델 revision snapshot이 올바르지 않습니다.")
        if len({model_id for model_id, _revision in revisions}) != len(revisions):
            raise IcpInputError("ICP 모델 revision snapshot에 중복 모델이 있습니다.")
        algorithm_version = str(self.algorithm_version).strip()
        if algorithm_version != ICP_ALGORITHM_VERSION:
            raise IcpInputError("ICP 알고리즘 버전이 현재 공개 버전과 다릅니다.")
        generated_fingerprint = _canonical_fingerprint(
            {
                "moving_model_id": moving_model_id,
                "reference_model_ids": reference_model_ids,
                "moving_cloud": self.moving_cloud.fingerprint,
                "moving_cloud_frame": moving_cloud_frame.value,
                "reference_clouds": [cloud.fingerprint for cloud in clouds],
                "initial_alignment": _array_fingerprint(self.initial_alignment.matrix_float64),
                "marker_uniform_scale": marker_uniform_scale,
                "marker_max_error": marker_max_error,
                "marker_rms": marker_rms,
                "marker_signature": marker_signature,
                "model_revision_snapshot": revisions,
                "parameters": parameters.fingerprint,
                "algorithm_version": algorithm_version,
            }
        )
        supplied_fingerprint = str(self.input_fingerprint).strip()
        if supplied_fingerprint and supplied_fingerprint != generated_fingerprint:
            raise IcpInputError("ICP 입력 fingerprint가 snapshot 내용과 다릅니다.")
        object.__setattr__(self, "moving_model_id", moving_model_id)
        object.__setattr__(self, "reference_model_ids", reference_model_ids)
        object.__setattr__(self, "reference_clouds", clouds)
        object.__setattr__(self, "marker_uniform_scale", marker_uniform_scale)
        object.__setattr__(self, "marker_max_error", marker_max_error)
        object.__setattr__(self, "marker_rms", marker_rms)
        object.__setattr__(self, "marker_reference_project", reference_markers)
        object.__setattr__(self, "marker_moving_project", moving_markers)
        object.__setattr__(self, "marker_signature", marker_signature)
        object.__setattr__(self, "model_revision_snapshot", revisions)
        object.__setattr__(self, "input_fingerprint", generated_fingerprint)
        object.__setattr__(self, "algorithm_version", algorithm_version)
        object.__setattr__(self, "moving_cloud_frame", moving_cloud_frame)

    @property
    def initial_alignment_float64(self) -> np.ndarray:
        """worker·manifest 호환을 위해 초기 Sim(3) 행렬을 반환합니다."""

        return self.initial_alignment.matrix_float64

    def to_mapping(self) -> dict[str, object]:
        """NPY 파일 경로를 제외한 worker manifest 메타데이터를 반환합니다."""

        return {
            "moving_model_id": self.moving_model_id,
            "reference_model_ids": list(self.reference_model_ids),
            "moving_cloud_frame": self.moving_cloud_frame.value,
            "initial_alignment_4x4": self.initial_alignment.matrix_float64.tolist(),
            "marker_uniform_scale": self.marker_uniform_scale,
            "marker_max_error": self.marker_max_error,
            "marker_rms": self.marker_rms,
            "marker_signature": self.marker_signature,
            "model_revision_snapshot": [list(item) for item in self.model_revision_snapshot],
            "input_fingerprint": self.input_fingerprint,
            "parameters": self.parameters.to_mapping(),
            "algorithm_version": self.algorithm_version,
        }


@dataclass(frozen=True, slots=True)
class IcpProgressSnapshot:
    """worker가 main Blender에 전달할 가벼운 진행 상태입니다."""

    stage: str
    level_name: str = ""
    iteration: int = 0
    max_iterations: int = 0
    progress: float = 0.0
    message: str = ""
    level_complete: bool = False
    stable_alignment_float64: np.ndarray | None = None
    objective_kind: IcpObjectiveKind | str | None = None
    maximum_correspondence_distance: float | None = None
    correspondence_count: int | None = None
    point_to_point_rmse: float | None = None
    point_to_plane_rmse: float | None = None
    normal_valid_count: int | None = None
    normal_angle_pass_count: int | None = None
    normal_angle_pass_ratio: float | None = None
    normal_radius: float | None = None
    moving_sample_count: int | None = None
    reference_sample_count: int | None = None

    def __post_init__(self) -> None:
        """진행률과 수치 검증된 최선 후보 안정 행렬을 검증합니다."""

        stage = str(self.stage).strip()
        level_name = str(self.level_name).strip()
        message = str(self.message).strip()
        iteration = _strict_nonnegative_int(self.iteration, "ICP 진행 iteration")
        max_iterations = _strict_nonnegative_int(self.max_iterations, "ICP 진행 최대 iteration")
        progress = _strict_finite_float(self.progress, "ICP 진행률")
        if not stage:
            raise IcpInputError("ICP 진행 단계가 비어 있습니다.")
        if iteration < 0 or max_iterations < 0 or iteration > max_iterations > 0:
            raise IcpInputError("ICP 진행 반복 횟수가 올바르지 않습니다.")
        if not isfinite(progress) or not 0.0 <= progress <= 1.0:
            raise IcpInputError("ICP 진행률은 0 이상 1 이하여야 합니다.")
        level_complete = _strict_exact_bool(self.level_complete, "ICP 레벨 완료 여부")
        stable = (
            None
            if self.stable_alignment_float64 is None
            else _freeze_matrix(self.stable_alignment_float64, "ICP 안정 alignment")
        )
        try:
            objective_kind = (
                None
                if self.objective_kind is None
                else self.objective_kind
                if isinstance(self.objective_kind, IcpObjectiveKind)
                else IcpObjectiveKind(str(self.objective_kind))
            )
        except ValueError as error:
            raise IcpInputError("ICP 진행 목적함수가 올바르지 않습니다.") from error
        optional_floats = {
            "maximum_correspondence_distance": self.maximum_correspondence_distance,
            "point_to_point_rmse": self.point_to_point_rmse,
            "point_to_plane_rmse": self.point_to_plane_rmse,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
            "normal_radius": self.normal_radius,
        }
        normalized_floats: dict[str, float | None] = {}
        for field_name, raw_value in optional_floats.items():
            normalized_floats[field_name] = (
                None if raw_value is None else _strict_finite_float(raw_value, f"ICP 진행 {field_name}")
            )
        optional_counts = {
            "normal_valid_count": self.normal_valid_count,
            "normal_angle_pass_count": self.normal_angle_pass_count,
            "moving_sample_count": self.moving_sample_count,
            "reference_sample_count": self.reference_sample_count,
            "correspondence_count": self.correspondence_count,
        }
        normalized_counts: dict[str, int | None] = {}
        for field_name, raw_value in optional_counts.items():
            normalized_counts[field_name] = (
                None if raw_value is None else _strict_nonnegative_int(raw_value, f"ICP 진행 {field_name}")
            )
        if any(value is not None and value < 0.0 for value in normalized_floats.values()):
            raise IcpInputError("ICP 진행 수치 지표는 0 이상이어야 합니다.")
        ratio = normalized_floats["normal_angle_pass_ratio"]
        if ratio is not None and ratio > 1.0:
            raise IcpInputError("ICP 진행 노멀 각도 통과 비율은 1 이하여야 합니다.")
        normal_valid = normalized_counts["normal_valid_count"]
        normal_angle = normalized_counts["normal_angle_pass_count"]
        if normal_valid is not None and normal_angle is not None and normal_angle > normal_valid:
            raise IcpInputError("ICP 진행 노멀 각도 통과 수가 유효 수보다 클 수 없습니다.")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "level_name", level_name)
        object.__setattr__(self, "iteration", iteration)
        object.__setattr__(self, "max_iterations", max_iterations)
        object.__setattr__(self, "progress", progress)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "level_complete", level_complete)
        object.__setattr__(self, "stable_alignment_float64", stable)
        object.__setattr__(self, "objective_kind", objective_kind)
        for field_name, normalized in normalized_floats.items():
            object.__setattr__(self, field_name, normalized)
        for field_name, normalized in normalized_counts.items():
            object.__setattr__(self, field_name, normalized)

    def to_mapping(self) -> dict[str, object]:
        """progress.json에 기록할 안전한 원시값 사전을 반환합니다."""

        result: dict[str, object] = {
            "stage": self.stage,
            "level_name": self.level_name,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "progress": self.progress,
            "message": self.message,
            "level_complete": self.level_complete,
        }
        if self.stable_alignment_float64 is not None:
            result["stable_alignment_4x4"] = self.stable_alignment_float64.tolist()
        if self.objective_kind is not None:
            result["objective_kind"] = self.objective_kind.value
        for field_name in (
            "maximum_correspondence_distance",
            "correspondence_count",
            "point_to_point_rmse",
            "point_to_plane_rmse",
            "normal_valid_count",
            "normal_angle_pass_count",
            "normal_angle_pass_ratio",
            "normal_radius",
            "moving_sample_count",
            "reference_sample_count",
        ):
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpProgressSnapshot":
        """worker progress.json 사전에서 검증된 진행 상태를 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP progress는 사전이어야 합니다.")
        try:
            return cls(
                stage=str(value["stage"]),
                level_name=str(value.get("level_name", "")),
                iteration=_strict_nonnegative_int(value.get("iteration", 0), "ICP 진행 iteration"),
                max_iterations=_strict_nonnegative_int(value.get("max_iterations", 0), "ICP 진행 최대 iteration"),
                progress=_strict_finite_float(value.get("progress", 0.0), "ICP 진행률"),
                message=str(value.get("message", "")),
                level_complete=_strict_exact_bool(value.get("level_complete", False), "ICP 레벨 완료 여부"),
                stable_alignment_float64=value.get("stable_alignment_4x4"),
                objective_kind=(
                    IcpObjectiveKind(str(value["objective_kind"]))
                    if value.get("objective_kind") is not None
                    else None
                ),
                maximum_correspondence_distance=(
                    _strict_finite_float(value["maximum_correspondence_distance"], "ICP 진행 maximum_correspondence_distance")
                    if value.get("maximum_correspondence_distance") is not None
                    else None
                ),
                correspondence_count=(
                    _strict_nonnegative_int(value["correspondence_count"], "ICP 진행 correspondence_count")
                    if value.get("correspondence_count") is not None
                    else None
                ),
                point_to_point_rmse=(
                    _strict_finite_float(value["point_to_point_rmse"], "ICP 진행 point_to_point_rmse")
                    if value.get("point_to_point_rmse") is not None
                    else None
                ),
                point_to_plane_rmse=(
                    _strict_finite_float(value["point_to_plane_rmse"], "ICP 진행 point_to_plane_rmse")
                    if value.get("point_to_plane_rmse") is not None
                    else None
                ),
                normal_valid_count=(
                    _strict_nonnegative_int(value["normal_valid_count"], "ICP 진행 normal_valid_count")
                    if value.get("normal_valid_count") is not None
                    else None
                ),
                normal_angle_pass_count=(
                    _strict_nonnegative_int(value["normal_angle_pass_count"], "ICP 진행 normal_angle_pass_count")
                    if value.get("normal_angle_pass_count") is not None
                    else None
                ),
                normal_angle_pass_ratio=(
                    _strict_finite_float(value["normal_angle_pass_ratio"], "ICP 진행 normal_angle_pass_ratio")
                    if value.get("normal_angle_pass_ratio") is not None
                    else None
                ),
                normal_radius=(
                    _strict_finite_float(value["normal_radius"], "ICP 진행 normal_radius")
                    if value.get("normal_radius") is not None
                    else None
                ),
                moving_sample_count=(
                    _strict_nonnegative_int(value["moving_sample_count"], "ICP 진행 moving_sample_count")
                    if value.get("moving_sample_count") is not None
                    else None
                ),
                reference_sample_count=(
                    _strict_nonnegative_int(value["reference_sample_count"], "ICP 진행 reference_sample_count")
                    if value.get("reference_sample_count") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP progress 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpIterationMetrics:
    """한 ICP 반복에서 사용자 품질과 수렴 판정에 쓸 지표를 보관합니다."""

    level_name: str
    iteration: int
    query_count: int
    maximum_distance_count: int
    unique_target_count: int
    inlier_count: int
    fitness: float
    rmse: float
    median_residual: float
    p90_residual: float
    delta_translation: float
    delta_rotation_degrees: float
    elapsed_seconds: float
    correspondence_signature: str = ""
    effective_trim_fraction: float = 1.0
    objective_kind: IcpObjectiveKind | str = IcpObjectiveKind.POINT_TO_POINT
    point_to_plane_rmse: float = 0.0
    point_to_point_rmse: float = 0.0
    normal_valid_count: int = 0
    normal_angle_pass_count: int = 0
    normal_angle_pass_ratio: float = 1.0
    maximum_correspondence_distance: float = 0.0
    linear_system_rank: int = 0
    linear_system_condition: float = 0.0
    best_candidate_updated: bool = False
    stop_reason: IcpSolverStopReason | str = IcpSolverStopReason.NONE

    def __post_init__(self) -> None:
        """반복 통계의 정수·비율·거리 값을 유한한 불변 값으로 검증합니다."""

        level_name = str(self.level_name).strip().lower()
        iteration = _strict_nonnegative_int(self.iteration, "ICP 반복 iteration")
        try:
            objective_kind = (
                self.objective_kind
                if isinstance(self.objective_kind, IcpObjectiveKind)
                else IcpObjectiveKind(str(self.objective_kind))
            )
            stop_reason = (
                self.stop_reason
                if isinstance(self.stop_reason, IcpSolverStopReason)
                else IcpSolverStopReason(str(self.stop_reason))
            )
        except ValueError as error:
            raise IcpInputError("ICP 반복 목적함수 또는 종료 사유가 올바르지 않습니다.") from error
        counts = {
            "query_count": _strict_nonnegative_int(self.query_count, "ICP 반복 query 수"),
            "maximum_distance_count": _strict_nonnegative_int(self.maximum_distance_count, "ICP 반복 최대 거리 통과 수"),
            "unique_target_count": _strict_nonnegative_int(self.unique_target_count, "ICP 반복 고유 target 수"),
            "inlier_count": _strict_nonnegative_int(self.inlier_count, "ICP 반복 inlier 수"),
        }
        values = {
            "fitness": _strict_finite_float(self.fitness, "ICP 반복 fitness"),
            "rmse": _strict_finite_float(self.rmse, "ICP 반복 RMSE"),
            "median_residual": _strict_finite_float(self.median_residual, "ICP 반복 median residual"),
            "p90_residual": _strict_finite_float(self.p90_residual, "ICP 반복 p90 residual"),
            "delta_translation": _strict_finite_float(self.delta_translation, "ICP 반복 이동량"),
            "delta_rotation_degrees": _strict_finite_float(self.delta_rotation_degrees, "ICP 반복 회전량"),
            "elapsed_seconds": _strict_finite_float(self.elapsed_seconds, "ICP 반복 경과 시간"),
            "effective_trim_fraction": _strict_finite_float(self.effective_trim_fraction, "ICP 실제 trim 비율"),
            "point_to_plane_rmse": _strict_finite_float(self.point_to_plane_rmse, "ICP 반복 plane RMSE"),
            "point_to_point_rmse": _strict_finite_float(self.point_to_point_rmse, "ICP 반복 point RMSE"),
            "normal_angle_pass_ratio": _strict_finite_float(self.normal_angle_pass_ratio, "ICP 노멀 각도 통과 비율"),
            "maximum_correspondence_distance": _strict_finite_float(
                self.maximum_correspondence_distance,
                "ICP 반복 최대 대응 거리",
            ),
            "linear_system_condition": _strict_finite_float(self.linear_system_condition, "ICP 선형계 condition"),
        }
        normal_valid_count = _strict_nonnegative_int(self.normal_valid_count, "ICP 노멀 유효 수")
        normal_angle_pass_count = _strict_nonnegative_int(
            self.normal_angle_pass_count,
            "ICP 노멀 각도 통과 수",
        )
        linear_system_rank = _strict_nonnegative_int(self.linear_system_rank, "ICP 선형계 rank")
        if not level_name or iteration < 0 or any(count < 0 for count in counts.values()):
            raise IcpInputError("ICP 반복 정수 지표가 올바르지 않습니다.")
        if counts["maximum_distance_count"] > counts["query_count"]:
            raise IcpInputError("최대 거리 통과 수가 query 수보다 클 수 없습니다.")
        if counts["unique_target_count"] > counts["maximum_distance_count"]:
            raise IcpInputError("고유 target 수가 최대 거리 통과 수보다 클 수 없습니다.")
        if counts["inlier_count"] > counts["unique_target_count"]:
            raise IcpInputError("inlier 수가 고유 target 수보다 클 수 없습니다.")
        if not all(isfinite(value) for value in values.values()):
            raise IcpInputError("ICP 반복 실수 지표는 유한해야 합니다.")
        if not 0.0 <= values["fitness"] <= 1.0 or not 0.0 < values["effective_trim_fraction"] <= 1.0 or any(
            values[name] < 0.0 for name in values if name != "delta_rotation_degrees"
        ):
            raise IcpInputError("ICP 반복 지표 범위가 올바르지 않습니다.")
        if values["delta_rotation_degrees"] < 0.0:
            raise IcpInputError("ICP 회전 변화량은 음수일 수 없습니다.")
        if (
            values["point_to_plane_rmse"] < 0.0
            or values["point_to_point_rmse"] < 0.0
            or values["maximum_correspondence_distance"] < 0.0
            or values["linear_system_condition"] < 0.0
            or not 0.0 <= values["normal_angle_pass_ratio"] <= 1.0
            or normal_valid_count > counts["maximum_distance_count"]
            or normal_angle_pass_count > normal_valid_count
        ):
            raise IcpInputError("ICP 노멀·선형계 반복 지표 범위가 올바르지 않습니다.")
        object.__setattr__(self, "level_name", level_name)
        object.__setattr__(self, "iteration", iteration)
        for name, value in counts.items():
            object.__setattr__(self, name, value)
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "correspondence_signature", str(self.correspondence_signature))
        object.__setattr__(self, "objective_kind", objective_kind)
        object.__setattr__(self, "point_to_plane_rmse", values["point_to_plane_rmse"])
        object.__setattr__(self, "point_to_point_rmse", values["point_to_point_rmse"])
        object.__setattr__(self, "normal_valid_count", normal_valid_count)
        object.__setattr__(self, "normal_angle_pass_count", normal_angle_pass_count)
        object.__setattr__(self, "normal_angle_pass_ratio", values["normal_angle_pass_ratio"])
        object.__setattr__(self, "maximum_correspondence_distance", values["maximum_correspondence_distance"])
        object.__setattr__(self, "linear_system_rank", linear_system_rank)
        object.__setattr__(self, "linear_system_condition", values["linear_system_condition"])
        object.__setattr__(
            self,
            "best_candidate_updated",
            _strict_exact_bool(self.best_candidate_updated, "ICP 최선 후보 갱신 여부"),
        )
        object.__setattr__(self, "stop_reason", stop_reason)

    def to_mapping(self) -> dict[str, object]:
        """결과 JSON의 반복 통계 사전을 반환합니다."""

        return {
            "level_name": self.level_name,
            "iteration": self.iteration,
            "query_count": self.query_count,
            "maximum_distance_count": self.maximum_distance_count,
            "unique_target_count": self.unique_target_count,
            "inlier_count": self.inlier_count,
            "fitness": self.fitness,
            "rmse": self.rmse,
            "median_residual": self.median_residual,
            "p90_residual": self.p90_residual,
            "delta_translation": self.delta_translation,
            "delta_rotation_degrees": self.delta_rotation_degrees,
            "elapsed_seconds": self.elapsed_seconds,
            "correspondence_signature": self.correspondence_signature,
            "effective_trim_fraction": self.effective_trim_fraction,
            "objective_kind": self.objective_kind.value,
            "point_to_plane_rmse": self.point_to_plane_rmse,
            "point_to_point_rmse": self.point_to_point_rmse,
            "normal_valid_count": self.normal_valid_count,
            "normal_angle_pass_count": self.normal_angle_pass_count,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
            "maximum_correspondence_distance": self.maximum_correspondence_distance,
            "linear_system_rank": self.linear_system_rank,
            "linear_system_condition": self.linear_system_condition,
            "best_candidate_updated": self.best_candidate_updated,
            "stop_reason": self.stop_reason.value,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpIterationMetrics":
        """결과 JSON의 반복 통계 사전에서 불변 값을 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 반복 결과는 사전이어야 합니다.")
        try:
            return cls(
                level_name=str(value["level_name"]),
                iteration=_strict_nonnegative_int(value["iteration"], "ICP 반복 iteration"),
                query_count=_strict_nonnegative_int(value["query_count"], "ICP 반복 query 수"),
                maximum_distance_count=_strict_nonnegative_int(value["maximum_distance_count"], "ICP 반복 최대 거리 통과 수"),
                unique_target_count=_strict_nonnegative_int(value["unique_target_count"], "ICP 반복 고유 target 수"),
                inlier_count=_strict_nonnegative_int(value["inlier_count"], "ICP 반복 inlier 수"),
                fitness=_strict_finite_float(value["fitness"], "ICP 반복 fitness"),
                rmse=_strict_finite_float(value["rmse"], "ICP 반복 RMSE"),
                median_residual=_strict_finite_float(value["median_residual"], "ICP 반복 median residual"),
                p90_residual=_strict_finite_float(value["p90_residual"], "ICP 반복 p90 residual"),
                delta_translation=_strict_finite_float(value["delta_translation"], "ICP 반복 이동량"),
                delta_rotation_degrees=_strict_finite_float(value["delta_rotation_degrees"], "ICP 반복 회전량"),
                elapsed_seconds=_strict_finite_float(value["elapsed_seconds"], "ICP 반복 경과 시간"),
                correspondence_signature=str(value.get("correspondence_signature", "")),
                effective_trim_fraction=_strict_finite_float(value.get("effective_trim_fraction", 1.0), "ICP 실제 trim 비율"),
                objective_kind=IcpObjectiveKind(
                    str(value.get("objective_kind", IcpObjectiveKind.POINT_TO_POINT.value))
                ),
                point_to_plane_rmse=_strict_finite_float(
                    value.get("point_to_plane_rmse", 0.0),
                    "ICP 반복 plane RMSE",
                ),
                point_to_point_rmse=_strict_finite_float(
                    value.get("point_to_point_rmse", value["rmse"]),
                    "ICP 반복 point RMSE",
                ),
                normal_valid_count=_strict_nonnegative_int(
                    value.get("normal_valid_count", 0),
                    "ICP 노멀 유효 수",
                ),
                normal_angle_pass_count=_strict_nonnegative_int(
                    value.get("normal_angle_pass_count", 0),
                    "ICP 노멀 각도 통과 수",
                ),
                normal_angle_pass_ratio=_strict_finite_float(
                    value.get("normal_angle_pass_ratio", 1.0),
                    "ICP 노멀 각도 통과 비율",
                ),
                maximum_correspondence_distance=_strict_finite_float(
                    value.get("maximum_correspondence_distance", 0.0),
                    "ICP 반복 최대 대응 거리",
                ),
                linear_system_rank=_strict_nonnegative_int(
                    value.get("linear_system_rank", 0),
                    "ICP 선형계 rank",
                ),
                linear_system_condition=_strict_finite_float(
                    value.get("linear_system_condition", 0.0), "ICP 선형계 condition"),
                best_candidate_updated=_strict_exact_bool(
                    value.get("best_candidate_updated", False),
                    "ICP 최선 후보 갱신 여부",
                ),
                stop_reason=IcpSolverStopReason(
                    str(value.get("stop_reason", IcpSolverStopReason.NONE.value))
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 반복 결과 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpLevelResult:
    """한 해상도 레벨의 샘플·gate·반복 결과를 보관합니다."""

    level_name: str
    voxel_size: float
    maximum_correspondence_distance: float
    moving_sample_count: int
    reference_sample_count: int
    iterations: tuple[IcpIterationMetrics, ...]
    converged: bool
    effective_trim_fraction: float = 1.0
    start_maximum_correspondence_distance: float = 0.0
    end_maximum_correspondence_distance: float = 0.0
    best_iteration: int = 0
    stop_reason: IcpSolverStopReason | str = IcpSolverStopReason.NONE
    objective_kind: IcpObjectiveKind | str = IcpObjectiveKind.POINT_TO_POINT
    normal_radius: float = 0.0
    moving_normal_valid_count: int = 0
    reference_normal_valid_count: int = 0
    normal_angle_pass_ratio: float = 1.0

    def __post_init__(self) -> None:
        """해상도 레벨의 유한 수치와 반복 이름 일관성을 검증합니다."""

        level_name = str(self.level_name).strip().lower()
        voxel_size = _strict_finite_float(self.voxel_size, "ICP 레벨 voxel size")
        maximum_distance = _strict_finite_float(self.maximum_correspondence_distance, "ICP 레벨 최대 대응 거리")
        effective_trim_fraction = _strict_finite_float(self.effective_trim_fraction, "ICP 레벨 실제 trim 비율")
        moving_count = _strict_positive_int(self.moving_sample_count, "ICP 레벨 이동 sample 수")
        reference_count = _strict_positive_int(self.reference_sample_count, "ICP 레벨 기준 sample 수")
        start_distance = _strict_finite_float(
            self.start_maximum_correspondence_distance,
            "ICP 레벨 시작 최대 대응 거리",
        )
        end_distance = _strict_finite_float(
            self.end_maximum_correspondence_distance,
            "ICP 레벨 종료 최대 대응 거리",
        )
        best_iteration = _strict_nonnegative_int(self.best_iteration, "ICP 레벨 최선 반복")
        normal_radius = _strict_finite_float(self.normal_radius, "ICP 레벨 노멀 반경")
        moving_normal_valid_count = _strict_nonnegative_int(
            self.moving_normal_valid_count,
            "ICP 레벨 P 노멀 유효 수",
        )
        reference_normal_valid_count = _strict_nonnegative_int(
            self.reference_normal_valid_count,
            "ICP 레벨 A 노멀 유효 수",
        )
        normal_angle_pass_ratio = _strict_finite_float(
            self.normal_angle_pass_ratio,
            "ICP 레벨 노멀 각도 통과 비율",
        )
        try:
            stop_reason = (
                self.stop_reason
                if isinstance(self.stop_reason, IcpSolverStopReason)
                else IcpSolverStopReason(str(self.stop_reason))
            )
            objective_kind = (
                self.objective_kind
                if isinstance(self.objective_kind, IcpObjectiveKind)
                else IcpObjectiveKind(str(self.objective_kind))
            )
        except ValueError as error:
            raise IcpInputError("ICP 레벨 종료 사유 또는 목적함수가 올바르지 않습니다.") from error
        iterations = tuple(self.iterations)
        if not level_name or voxel_size <= 0.0:
            raise IcpInputError("ICP 레벨 voxel size가 올바르지 않습니다.")
        if maximum_distance <= 0.0:
            raise IcpInputError("ICP 레벨 최대 대응 거리가 올바르지 않습니다.")
        if moving_count < 1 or reference_count < 1:
            raise IcpInputError("ICP 레벨 sample 수는 양수여야 합니다.")
        if not all(isinstance(item, IcpIterationMetrics) for item in iterations):
            raise IcpInputError("ICP 레벨 반복 결과 형식이 올바르지 않습니다.")
        if any(item.level_name != level_name for item in iterations):
            raise IcpInputError("ICP 레벨과 반복 결과 이름이 다릅니다.")
        if not 0.0 < effective_trim_fraction <= 1.0:
            raise IcpInputError("ICP 레벨 실제 trim 비율 범위가 올바르지 않습니다.")
        if (
            start_distance < 0.0
            or end_distance < 0.0
            or normal_radius < 0.0
            or not 0.0 <= normal_angle_pass_ratio <= 1.0
            or moving_normal_valid_count > moving_count
            or reference_normal_valid_count > reference_count
        ):
            raise IcpInputError("ICP 레벨 노멀·반경 지표 범위가 올바르지 않습니다.")
        object.__setattr__(self, "level_name", level_name)
        object.__setattr__(self, "voxel_size", voxel_size)
        object.__setattr__(self, "maximum_correspondence_distance", maximum_distance)
        object.__setattr__(self, "moving_sample_count", moving_count)
        object.__setattr__(self, "reference_sample_count", reference_count)
        object.__setattr__(self, "iterations", iterations)
        object.__setattr__(self, "converged", _strict_exact_bool(self.converged, "ICP 레벨 수렴 여부"))
        object.__setattr__(self, "effective_trim_fraction", effective_trim_fraction)
        object.__setattr__(self, "start_maximum_correspondence_distance", start_distance)
        object.__setattr__(self, "end_maximum_correspondence_distance", end_distance)
        object.__setattr__(self, "best_iteration", best_iteration)
        object.__setattr__(self, "stop_reason", stop_reason)
        object.__setattr__(self, "objective_kind", objective_kind)
        object.__setattr__(self, "normal_radius", normal_radius)
        object.__setattr__(self, "moving_normal_valid_count", moving_normal_valid_count)
        object.__setattr__(self, "reference_normal_valid_count", reference_normal_valid_count)
        object.__setattr__(self, "normal_angle_pass_ratio", normal_angle_pass_ratio)

    @property
    def final_iteration(self) -> IcpIterationMetrics | None:
        """레벨의 마지막 성공 반복을 반환합니다."""

        return self.iterations[-1] if self.iterations else None

    def to_mapping(self) -> dict[str, object]:
        """결과 JSON에 기록할 해상도 레벨 사전을 반환합니다."""

        return {
            "level_name": self.level_name,
            "voxel_size": self.voxel_size,
            "maximum_correspondence_distance": self.maximum_correspondence_distance,
            "moving_sample_count": self.moving_sample_count,
            "reference_sample_count": self.reference_sample_count,
            "iterations": [item.to_mapping() for item in self.iterations],
            "converged": self.converged,
            "effective_trim_fraction": self.effective_trim_fraction,
            "start_maximum_correspondence_distance": self.start_maximum_correspondence_distance,
            "end_maximum_correspondence_distance": self.end_maximum_correspondence_distance,
            "best_iteration": self.best_iteration,
            "stop_reason": self.stop_reason.value,
            "objective_kind": self.objective_kind.value,
            "normal_radius": self.normal_radius,
            "moving_normal_valid_count": self.moving_normal_valid_count,
            "reference_normal_valid_count": self.reference_normal_valid_count,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpLevelResult":
        """결과 JSON의 해상도 레벨 사전에서 불변 결과를 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 레벨 결과는 사전이어야 합니다.")
        try:
            return cls(
                level_name=str(value["level_name"]),
                voxel_size=_strict_finite_float(value["voxel_size"], "ICP 레벨 voxel size"),
                maximum_correspondence_distance=_strict_finite_float(value["maximum_correspondence_distance"], "ICP 레벨 최대 대응 거리"),
                moving_sample_count=_strict_positive_int(value["moving_sample_count"], "ICP 레벨 이동 sample 수"),
                reference_sample_count=_strict_positive_int(value["reference_sample_count"], "ICP 레벨 기준 sample 수"),
                iterations=tuple(
                    IcpIterationMetrics.from_mapping(item)
                    for item in _strict_mapping_sequence(
                        value.get("iterations", ()),
                        "ICP 레벨 반복 결과",
                    )
                ),
                converged=_strict_exact_bool(value["converged"], "ICP 레벨 수렴 여부"),
                effective_trim_fraction=_strict_finite_float(value.get("effective_trim_fraction", 1.0), "ICP 레벨 실제 trim 비율"),
                start_maximum_correspondence_distance=_strict_finite_float(
                    value.get("start_maximum_correspondence_distance", 0.0),
                    "ICP 레벨 시작 최대 대응 거리",
                ),
                end_maximum_correspondence_distance=_strict_finite_float(
                    value.get("end_maximum_correspondence_distance", 0.0),
                    "ICP 레벨 종료 최대 대응 거리",
                ),
                best_iteration=_strict_nonnegative_int(
                    value.get("best_iteration", 0),
                    "ICP 레벨 최선 반복",
                ),
                stop_reason=IcpSolverStopReason(
                    str(value.get("stop_reason", IcpSolverStopReason.NONE.value))
                ),
                objective_kind=IcpObjectiveKind(
                    str(value.get("objective_kind", IcpObjectiveKind.POINT_TO_POINT.value))
                ),
                normal_radius=_strict_finite_float(
                    value.get("normal_radius", 0.0),
                    "ICP 레벨 노멀 반경",
                ),
                moving_normal_valid_count=_strict_nonnegative_int(
                    value.get("moving_normal_valid_count", 0),
                    "ICP 레벨 P 노멀 유효 수",
                ),
                reference_normal_valid_count=_strict_nonnegative_int(
                    value.get("reference_normal_valid_count", 0),
                    "ICP 레벨 A 노멀 유효 수",
                ),
                normal_angle_pass_ratio=_strict_finite_float(
                    value.get("normal_angle_pass_ratio", 1.0),
                    "ICP 레벨 노멀 각도 통과 비율",
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 레벨 결과 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpPerReferenceMetrics:
    """다중 A 기준 모델별 최종 correspondence 기여도를 보관합니다."""

    model_id: str
    correspondence_count: int
    fraction: float
    overlap_moving_coverage: float = 0.0

    def __post_init__(self) -> None:
        """기준 모델 기여도 값의 범위를 검증합니다."""

        model_id = str(self.model_id).strip()
        count = _strict_nonnegative_int(self.correspondence_count, "ICP 기준 모델 correspondence 수")
        fraction = _strict_finite_float(self.fraction, "ICP 기준 모델 기여 비율")
        coverage = _strict_finite_float(self.overlap_moving_coverage, "ICP 기준 모델 overlap coverage")
        if not model_id or count < 0 or not 0.0 <= fraction <= 1.0 or not 0.0 <= coverage <= 1.0:
            raise IcpInputError("ICP 기준 모델별 기여도 값이 올바르지 않습니다.")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "correspondence_count", count)
        object.__setattr__(self, "fraction", fraction)
        object.__setattr__(self, "overlap_moving_coverage", coverage)

    def to_mapping(self) -> dict[str, object]:
        """결과 JSON의 모델별 기여도 사전을 반환합니다."""

        return {
            "model_id": self.model_id,
            "correspondence_count": self.correspondence_count,
            "fraction": self.fraction,
            "overlap_moving_coverage": self.overlap_moving_coverage,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpPerReferenceMetrics":
        """결과 JSON에서 모델별 기여도를 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 기준 모델 기여도는 사전이어야 합니다.")
        try:
            return cls(
                model_id=str(value["model_id"]),
                correspondence_count=_strict_nonnegative_int(value["correspondence_count"], "ICP 기준 모델 correspondence 수"),
                fraction=_strict_finite_float(value["fraction"], "ICP 기준 모델 기여 비율"),
                overlap_moving_coverage=_strict_finite_float(value.get("overlap_moving_coverage", 0.0), "ICP 기준 모델 overlap coverage"),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 기준 모델 기여도 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpEvaluationMetrics:
    """고정 평가 표본에서 비교한 마커 기준 또는 ICP 후보 지표입니다."""

    point_to_point_rmse: float
    point_to_plane_rmse: float
    fitness: float
    reciprocal_ratio: float
    inlier_count: int
    query_count: int
    maximum_correspondence_distance: float
    normal_valid_ratio: float
    normal_angle_pass_ratio: float
    evaluation_fingerprint: str

    def __post_init__(self) -> None:
        """평가 수치와 고정 표본 지문을 엄격한 불변 값으로 검증합니다."""

        values = {
            "point_to_point_rmse": _strict_finite_float(self.point_to_point_rmse, "ICP 평가 point RMSE"),
            "point_to_plane_rmse": _strict_finite_float(self.point_to_plane_rmse, "ICP 평가 plane RMSE"),
            "fitness": _strict_finite_float(self.fitness, "ICP 평가 fitness"),
            "reciprocal_ratio": _strict_finite_float(self.reciprocal_ratio, "ICP 평가 reciprocal 비율"),
            "maximum_correspondence_distance": _strict_finite_float(
                self.maximum_correspondence_distance,
                "ICP 평가 최대 대응 거리",
            ),
            "normal_valid_ratio": _strict_finite_float(self.normal_valid_ratio, "ICP 평가 노멀 유효 비율"),
            "normal_angle_pass_ratio": _strict_finite_float(
                self.normal_angle_pass_ratio,
                "ICP 평가 노멀 각도 통과 비율",
            ),
        }
        inlier_count = _strict_nonnegative_int(self.inlier_count, "ICP 평가 inlier 수")
        query_count = _strict_nonnegative_int(self.query_count, "ICP 평가 query 수")
        if (
            any(value < 0.0 for value in values.values())
            or values["fitness"] > 1.0
            or values["reciprocal_ratio"] > 1.0
            or values["normal_valid_ratio"] > 1.0
            or values["normal_angle_pass_ratio"] > 1.0
            or inlier_count > query_count
        ):
            raise IcpInputError("ICP 고정 평가 지표 범위가 올바르지 않습니다.")
        fingerprint = str(self.evaluation_fingerprint).strip()
        if not fingerprint:
            raise IcpInputError("ICP 고정 평가 표본 지문이 비어 있습니다.")
        for name, normalized in values.items():
            object.__setattr__(self, name, normalized)
        object.__setattr__(self, "inlier_count", inlier_count)
        object.__setattr__(self, "query_count", query_count)
        object.__setattr__(self, "evaluation_fingerprint", fingerprint)

    def to_mapping(self) -> dict[str, object]:
        """worker 결과 JSON에 쓸 고정 평가 지표 사전을 반환합니다."""

        return {
            "point_to_point_rmse": self.point_to_point_rmse,
            "point_to_plane_rmse": self.point_to_plane_rmse,
            "fitness": self.fitness,
            "reciprocal_ratio": self.reciprocal_ratio,
            "inlier_count": self.inlier_count,
            "query_count": self.query_count,
            "maximum_correspondence_distance": self.maximum_correspondence_distance,
            "normal_valid_ratio": self.normal_valid_ratio,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
            "evaluation_fingerprint": self.evaluation_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpEvaluationMetrics":
        """worker JSON의 고정 평가 지표를 엄격하게 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 고정 평가 지표는 사전이어야 합니다.")
        try:
            return cls(
                point_to_point_rmse=_strict_finite_float(value["point_to_point_rmse"], "ICP 평가 point RMSE"),
                point_to_plane_rmse=_strict_finite_float(value["point_to_plane_rmse"], "ICP 평가 plane RMSE"),
                fitness=_strict_finite_float(value["fitness"], "ICP 평가 fitness"),
                reciprocal_ratio=_strict_finite_float(value["reciprocal_ratio"], "ICP 평가 reciprocal 비율"),
                inlier_count=_strict_nonnegative_int(value["inlier_count"], "ICP 평가 inlier 수"),
                query_count=_strict_nonnegative_int(value["query_count"], "ICP 평가 query 수"),
                maximum_correspondence_distance=_strict_finite_float(
                    value["maximum_correspondence_distance"],
                    "ICP 평가 최대 대응 거리",
                ),
                normal_valid_ratio=_strict_finite_float(value["normal_valid_ratio"], "ICP 평가 노멀 유효 비율"),
                normal_angle_pass_ratio=_strict_finite_float(
                    value["normal_angle_pass_ratio"],
                    "ICP 평가 노멀 각도 통과 비율",
                ),
                evaluation_fingerprint=str(value["evaluation_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 고정 평가 지표 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpDensitySamplingMetrics:
    """공통 voxel과 macro-cell quota의 실제 표본 분포를 요약합니다."""

    shared_voxel_size: float
    macro_cell_divisions: int
    macro_cell_count: int
    occupied_macro_cell_count: int
    minimum_samples_per_macro_cell: int
    median_samples_per_macro_cell: float
    maximum_samples_per_macro_cell: int

    def __post_init__(self) -> None:
        """표본 분포 수치가 UI 로그에 안전하게 표시될 범위인지 검증합니다."""

        voxel_size = _strict_finite_float(self.shared_voxel_size, "ICP 공통 voxel 크기")
        divisions = _strict_positive_int(self.macro_cell_divisions, "ICP macro-cell 분할 수")
        macro_count = _strict_nonnegative_int(self.macro_cell_count, "ICP macro-cell 수")
        occupied_count = _strict_nonnegative_int(self.occupied_macro_cell_count, "ICP 점유 macro-cell 수")
        minimum_count = _strict_nonnegative_int(
            self.minimum_samples_per_macro_cell,
            "ICP macro-cell 최소 표본 수",
        )
        median_count = _strict_finite_float(
            self.median_samples_per_macro_cell,
            "ICP macro-cell 중앙 표본 수",
        )
        maximum_count = _strict_nonnegative_int(
            self.maximum_samples_per_macro_cell,
            "ICP macro-cell 최대 표본 수",
        )
        if (
            voxel_size <= 0.0
            or occupied_count > macro_count
            or median_count < 0.0
            or minimum_count > maximum_count
            or median_count > maximum_count
        ):
            raise IcpInputError("ICP macro-cell 표본 분포가 올바르지 않습니다.")
        object.__setattr__(self, "shared_voxel_size", voxel_size)
        object.__setattr__(self, "macro_cell_divisions", divisions)
        object.__setattr__(self, "macro_cell_count", macro_count)
        object.__setattr__(self, "occupied_macro_cell_count", occupied_count)
        object.__setattr__(self, "minimum_samples_per_macro_cell", minimum_count)
        object.__setattr__(self, "median_samples_per_macro_cell", median_count)
        object.__setattr__(self, "maximum_samples_per_macro_cell", maximum_count)

    def to_mapping(self) -> dict[str, object]:
        """worker 결과 JSON에 쓸 표본 분포 사전을 반환합니다."""

        return {
            "shared_voxel_size": self.shared_voxel_size,
            "macro_cell_divisions": self.macro_cell_divisions,
            "macro_cell_count": self.macro_cell_count,
            "occupied_macro_cell_count": self.occupied_macro_cell_count,
            "minimum_samples_per_macro_cell": self.minimum_samples_per_macro_cell,
            "median_samples_per_macro_cell": self.median_samples_per_macro_cell,
            "maximum_samples_per_macro_cell": self.maximum_samples_per_macro_cell,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpDensitySamplingMetrics":
        """worker JSON의 표본 분포 사전을 엄격하게 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 표본 분포는 사전이어야 합니다.")
        try:
            return cls(
                shared_voxel_size=_strict_finite_float(value["shared_voxel_size"], "ICP 공통 voxel 크기"),
                macro_cell_divisions=_strict_positive_int(value["macro_cell_divisions"], "ICP macro-cell 분할 수"),
                macro_cell_count=_strict_nonnegative_int(value["macro_cell_count"], "ICP macro-cell 수"),
                occupied_macro_cell_count=_strict_nonnegative_int(value["occupied_macro_cell_count"], "ICP 점유 macro-cell 수"),
                minimum_samples_per_macro_cell=_strict_nonnegative_int(
                    value["minimum_samples_per_macro_cell"],
                    "ICP macro-cell 최소 표본 수",
                ),
                median_samples_per_macro_cell=_strict_finite_float(
                    value["median_samples_per_macro_cell"],
                    "ICP macro-cell 중앙 표본 수",
                ),
                maximum_samples_per_macro_cell=_strict_nonnegative_int(
                    value["maximum_samples_per_macro_cell"],
                    "ICP macro-cell 최대 표본 수",
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 표본 분포 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpBestCandidate:
    """고정 평가에서 검증된 최선 ICP 후보와 위치 정보를 보관합니다."""

    delta_transform: Sim3Transform
    final_alignment: Sim3Transform
    level_name: str
    iteration: int
    objective_kind: IcpObjectiveKind | str
    point_to_plane_rmse: float
    point_to_point_rmse: float
    fitness: float
    inlier_count: int
    reciprocal_ratio: float
    normal_valid_ratio: float
    normal_angle_pass_ratio: float
    maximum_correspondence_distance: float
    correspondence_signature: str
    evaluation_fingerprint: str

    def __post_init__(self) -> None:
        """행렬·평가 지표·위치 식별자를 불변 후보로 고정합니다."""

        if not isinstance(self.delta_transform, Sim3Transform) or not isinstance(self.final_alignment, Sim3Transform):
            raise IcpInputError("ICP 최선 후보 transform 형식이 올바르지 않습니다.")
        level_name = str(self.level_name).strip().lower()
        iteration = _strict_nonnegative_int(self.iteration, "ICP 최선 후보 반복")
        try:
            objective_kind = (
                self.objective_kind
                if isinstance(self.objective_kind, IcpObjectiveKind)
                else IcpObjectiveKind(str(self.objective_kind))
            )
        except ValueError as error:
            raise IcpInputError("ICP 최선 후보 목적함수가 올바르지 않습니다.") from error
        values = {
            "point_to_plane_rmse": _strict_finite_float(self.point_to_plane_rmse, "ICP 최선 후보 plane RMSE"),
            "point_to_point_rmse": _strict_finite_float(self.point_to_point_rmse, "ICP 최선 후보 point RMSE"),
            "fitness": _strict_finite_float(self.fitness, "ICP 최선 후보 fitness"),
            "reciprocal_ratio": _strict_finite_float(self.reciprocal_ratio, "ICP 최선 후보 reciprocal 비율"),
            "normal_valid_ratio": _strict_finite_float(self.normal_valid_ratio, "ICP 최선 후보 노멀 유효 비율"),
            "normal_angle_pass_ratio": _strict_finite_float(
                self.normal_angle_pass_ratio,
                "ICP 최선 후보 노멀 각도 통과 비율",
            ),
            "maximum_correspondence_distance": _strict_finite_float(
                self.maximum_correspondence_distance,
                "ICP 최선 후보 최대 대응 거리",
            ),
        }
        inlier_count = _strict_nonnegative_int(self.inlier_count, "ICP 최선 후보 inlier 수")
        if (
            not level_name
            or any(value < 0.0 for value in values.values())
            or any(values[name] > 1.0 for name in ("fitness", "reciprocal_ratio", "normal_valid_ratio", "normal_angle_pass_ratio"))
        ):
            raise IcpInputError("ICP 최선 후보 지표 범위가 올바르지 않습니다.")
        correspondence_signature = str(self.correspondence_signature).strip()
        evaluation_fingerprint = str(self.evaluation_fingerprint).strip()
        if not correspondence_signature or not evaluation_fingerprint:
            raise IcpInputError("ICP 최선 후보 대응점·평가 지문이 비어 있습니다.")
        object.__setattr__(self, "level_name", level_name)
        object.__setattr__(self, "iteration", iteration)
        object.__setattr__(self, "objective_kind", objective_kind)
        object.__setattr__(self, "inlier_count", inlier_count)
        for name, normalized in values.items():
            object.__setattr__(self, name, normalized)
        object.__setattr__(self, "correspondence_signature", correspondence_signature)
        object.__setattr__(self, "evaluation_fingerprint", evaluation_fingerprint)

    def to_mapping(self) -> dict[str, object]:
        """worker 결과 JSON에 쓸 최선 후보 사전을 반환합니다."""

        return {
            "delta_4x4": self.delta_transform.matrix_float64.tolist(),
            "final_alignment_4x4": self.final_alignment.matrix_float64.tolist(),
            "level_name": self.level_name,
            "iteration": self.iteration,
            "objective_kind": self.objective_kind.value,
            "point_to_plane_rmse": self.point_to_plane_rmse,
            "point_to_point_rmse": self.point_to_point_rmse,
            "fitness": self.fitness,
            "inlier_count": self.inlier_count,
            "reciprocal_ratio": self.reciprocal_ratio,
            "normal_valid_ratio": self.normal_valid_ratio,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
            "maximum_correspondence_distance": self.maximum_correspondence_distance,
            "correspondence_signature": self.correspondence_signature,
            "evaluation_fingerprint": self.evaluation_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpBestCandidate":
        """worker JSON의 최선 후보를 엄격하게 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 최선 후보는 사전이어야 합니다.")
        try:
            return cls(
                delta_transform=Sim3Transform(matrix_float64=value["delta_4x4"]),
                final_alignment=Sim3Transform(matrix_float64=value["final_alignment_4x4"]),
                level_name=str(value["level_name"]),
                iteration=_strict_nonnegative_int(value["iteration"], "ICP 최선 후보 반복"),
                objective_kind=IcpObjectiveKind(str(value["objective_kind"])),
                point_to_plane_rmse=_strict_finite_float(value["point_to_plane_rmse"], "ICP 최선 후보 plane RMSE"),
                point_to_point_rmse=_strict_finite_float(value["point_to_point_rmse"], "ICP 최선 후보 point RMSE"),
                fitness=_strict_finite_float(value["fitness"], "ICP 최선 후보 fitness"),
                inlier_count=_strict_nonnegative_int(value["inlier_count"], "ICP 최선 후보 inlier 수"),
                reciprocal_ratio=_strict_finite_float(value["reciprocal_ratio"], "ICP 최선 후보 reciprocal 비율"),
                normal_valid_ratio=_strict_finite_float(value["normal_valid_ratio"], "ICP 최선 후보 노멀 유효 비율"),
                normal_angle_pass_ratio=_strict_finite_float(
                    value["normal_angle_pass_ratio"],
                    "ICP 최선 후보 노멀 각도 통과 비율",
                ),
                maximum_correspondence_distance=_strict_finite_float(
                    value["maximum_correspondence_distance"],
                    "ICP 최선 후보 최대 대응 거리",
                ),
                correspondence_signature=str(value["correspondence_signature"]),
                evaluation_fingerprint=str(value["evaluation_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 최선 후보 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpQualityMetrics:
    """최종 ICP 결과의 미리보기 허용 여부와 설명 가능한 품질 지표입니다."""

    status: IcpQualityStatus
    fitness: float
    rmse: float
    normalized_rmse_voxel: float
    normalized_rmse_diagonal: float
    median_residual: float
    p90_residual: float
    overlap_ratio: float
    reciprocal_ratio: float
    inlier_count: int
    query_count: int
    total_iterations: int
    delta_translation: float
    delta_rotation_degrees: float
    marker_uniform_scale: float
    final_uniform_scale: float
    scale_ratio: float
    scale_refine_status: IcpScaleRefineStatus
    scale_refine_reason: str
    marker_drift_rms: float
    elapsed_seconds: float
    per_reference_model: tuple[IcpPerReferenceMetrics, ...] = ()
    review_reasons: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    baseline_metrics: IcpEvaluationMetrics | None = None
    best_candidate_metrics: IcpEvaluationMetrics | None = None
    relative_improvement: float = 0.0
    best_level_name: str = ""
    best_iteration: int = 0
    solver_stop_reason: IcpSolverStopReason | str = IcpSolverStopReason.NONE
    point_to_plane_rmse: float = 0.0
    normal_valid_ratio: float = 1.0
    normal_angle_pass_ratio: float = 1.0
    density_sampling_metrics: IcpDensitySamplingMetrics | None = None
    user_review_required: bool = False

    def __post_init__(self) -> None:
        """품질 상태와 모든 수치 지표를 유한한 읽기 전용 결과로 검증합니다."""

        try:
            status = self.status if isinstance(self.status, IcpQualityStatus) else IcpQualityStatus(str(self.status))
            refine_status = (
                self.scale_refine_status
                if isinstance(self.scale_refine_status, IcpScaleRefineStatus)
                else IcpScaleRefineStatus(str(self.scale_refine_status))
            )
        except ValueError as error:
            raise IcpInputError("ICP 품질 상태가 올바르지 않습니다.") from error
        values = {
            "fitness": _strict_finite_float(self.fitness, "ICP 품질 fitness"),
            "rmse": _strict_finite_float(self.rmse, "ICP 품질 RMSE"),
            "normalized_rmse_voxel": _strict_finite_float(self.normalized_rmse_voxel, "ICP 품질 voxel 정규화 RMSE"),
            "normalized_rmse_diagonal": _strict_finite_float(self.normalized_rmse_diagonal, "ICP 품질 대각선 정규화 RMSE"),
            "median_residual": _strict_finite_float(self.median_residual, "ICP 품질 median residual"),
            "p90_residual": _strict_finite_float(self.p90_residual, "ICP 품질 p90 residual"),
            "overlap_ratio": _strict_finite_float(self.overlap_ratio, "ICP 품질 overlap 비율"),
            "reciprocal_ratio": _strict_finite_float(self.reciprocal_ratio, "ICP 품질 reciprocal 비율"),
            "delta_translation": _strict_finite_float(self.delta_translation, "ICP 품질 이동량"),
            "delta_rotation_degrees": _strict_finite_float(self.delta_rotation_degrees, "ICP 품질 회전량"),
            "marker_uniform_scale": _strict_finite_float(self.marker_uniform_scale, "ICP 품질 marker 스케일"),
            "final_uniform_scale": _strict_finite_float(self.final_uniform_scale, "ICP 품질 최종 스케일"),
            "scale_ratio": _strict_finite_float(self.scale_ratio, "ICP 품질 스케일 비율"),
            "marker_drift_rms": _strict_finite_float(self.marker_drift_rms, "ICP 품질 marker drift"),
            "elapsed_seconds": _strict_finite_float(self.elapsed_seconds, "ICP 품질 경과 시간"),
        }
        counts = {
            "inlier_count": _strict_nonnegative_int(self.inlier_count, "ICP 품질 inlier 수"),
            "query_count": _strict_nonnegative_int(self.query_count, "ICP 품질 query 수"),
            "total_iterations": _strict_nonnegative_int(self.total_iterations, "ICP 품질 총 반복 수"),
        }
        relative_improvement = _strict_finite_float(
            self.relative_improvement,
            "ICP 품질 상대 개선율",
        )
        point_to_plane_rmse = _strict_finite_float(
            self.point_to_plane_rmse,
            "ICP 품질 plane RMSE",
        )
        normal_valid_ratio = _strict_finite_float(
            self.normal_valid_ratio,
            "ICP 품질 노멀 유효 비율",
        )
        normal_angle_pass_ratio = _strict_finite_float(
            self.normal_angle_pass_ratio,
            "ICP 품질 노멀 각도 통과 비율",
        )
        best_iteration = _strict_nonnegative_int(self.best_iteration, "ICP 품질 최선 반복")
        try:
            solver_stop_reason = (
                self.solver_stop_reason
                if isinstance(self.solver_stop_reason, IcpSolverStopReason)
                else IcpSolverStopReason(str(self.solver_stop_reason))
            )
        except ValueError as error:
            raise IcpInputError("ICP 품질 solver 종료 사유가 올바르지 않습니다.") from error
        if not all(isfinite(value) for value in values.values()) or any(value < 0.0 for value in values.values()):
            raise IcpInputError("ICP 품질 실수 지표는 유한한 0 이상이어야 합니다.")
        if values["fitness"] > 1.0 or values["overlap_ratio"] > 1.0 or values["reciprocal_ratio"] > 1.0:
            raise IcpInputError("ICP 품질 비율은 1을 넘을 수 없습니다.")
        if any(value < 0 for value in counts.values()) or counts["inlier_count"] > counts["query_count"]:
            raise IcpInputError("ICP 품질 대응점 수가 올바르지 않습니다.")
        if values["marker_uniform_scale"] <= 0.0 or values["final_uniform_scale"] <= 0.0 or values["scale_ratio"] <= 0.0:
            raise IcpInputError("ICP 품질 스케일은 양수여야 합니다.")
        if (
            point_to_plane_rmse < 0.0
            or not 0.0 <= normal_valid_ratio <= 1.0
            or not 0.0 <= normal_angle_pass_ratio <= 1.0
        ):
            raise IcpInputError("ICP 품질 노멀·plane 지표 범위가 올바르지 않습니다.")
        expected_ratio = values["final_uniform_scale"] / values["marker_uniform_scale"]
        if not np.isclose(values["scale_ratio"], expected_ratio, rtol=0.0, atol=1.0e-9):
            raise IcpInputError("ICP 품질 scale ratio가 실제 스케일과 다릅니다.")
        per_reference = tuple(self.per_reference_model)
        if not all(isinstance(item, IcpPerReferenceMetrics) for item in per_reference):
            raise IcpInputError("ICP 기준 모델별 품질 형식이 올바르지 않습니다.")
        if len({item.model_id for item in per_reference}) != len(per_reference):
            raise IcpInputError("ICP 기준 모델별 품질 ID가 중복됩니다.")
        if per_reference:
            if sum(item.correspondence_count for item in per_reference) != counts["inlier_count"]:
                raise IcpInputError("ICP 기준 모델별 대응점 수 합이 최종 inlier 수와 다릅니다.")
            expected_fraction_sum = sum(item.fraction for item in per_reference)
            if not np.isclose(expected_fraction_sum, 1.0, rtol=0.0, atol=1.0e-9):
                raise IcpInputError("ICP 기준 모델별 대응점 비율 합은 1이어야 합니다.")
            for item in per_reference:
                expected_fraction = item.correspondence_count / counts["inlier_count"]
                if not np.isclose(item.fraction, expected_fraction, rtol=0.0, atol=1.0e-12):
                    raise IcpInputError("ICP 기준 모델별 대응점 비율이 count와 다릅니다.")
        reasons = tuple(str(reason).strip() for reason in self.review_reasons if str(reason).strip())
        rejection_reasons = tuple(
            str(reason).strip() for reason in self.rejection_reasons if str(reason).strip()
        )
        baseline_metrics = self.baseline_metrics
        best_candidate_metrics = self.best_candidate_metrics
        if baseline_metrics is not None and not isinstance(baseline_metrics, IcpEvaluationMetrics):
            raise IcpInputError("ICP 품질 baseline 지표 형식이 올바르지 않습니다.")
        if best_candidate_metrics is not None and not isinstance(best_candidate_metrics, IcpEvaluationMetrics):
            raise IcpInputError("ICP 품질 candidate 지표 형식이 올바르지 않습니다.")
        if (baseline_metrics is None) != (best_candidate_metrics is None):
            raise IcpInputError("ICP 품질 baseline과 candidate 지표는 함께 제공되어야 합니다.")
        if (
            baseline_metrics is not None
            and best_candidate_metrics is not None
            and baseline_metrics.evaluation_fingerprint != best_candidate_metrics.evaluation_fingerprint
        ):
            raise IcpInputError("ICP baseline과 candidate는 같은 고정 평가 표본을 사용해야 합니다.")
        density_sampling_metrics = self.density_sampling_metrics
        if density_sampling_metrics is not None and not isinstance(density_sampling_metrics, IcpDensitySamplingMetrics):
            raise IcpInputError("ICP 품질 밀도 표본 지표 형식이 올바르지 않습니다.")
        best_level_name = str(self.best_level_name).strip().lower()
        user_review_required = _strict_exact_bool(self.user_review_required, "ICP 사용자 검토 필요 여부")
        if status is IcpQualityStatus.REVIEW:
            user_review_required = True
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "scale_refine_status", refine_status)
        for name, value in values.items():
            object.__setattr__(self, name, value)
        for name, value in counts.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "scale_refine_reason", str(self.scale_refine_reason).strip())
        object.__setattr__(self, "per_reference_model", per_reference)
        object.__setattr__(self, "review_reasons", reasons)
        object.__setattr__(self, "rejection_reasons", rejection_reasons)
        object.__setattr__(self, "baseline_metrics", baseline_metrics)
        object.__setattr__(self, "best_candidate_metrics", best_candidate_metrics)
        object.__setattr__(self, "relative_improvement", relative_improvement)
        object.__setattr__(self, "best_level_name", best_level_name)
        object.__setattr__(self, "best_iteration", best_iteration)
        object.__setattr__(self, "solver_stop_reason", solver_stop_reason)
        object.__setattr__(self, "point_to_plane_rmse", point_to_plane_rmse)
        object.__setattr__(self, "normal_valid_ratio", normal_valid_ratio)
        object.__setattr__(self, "normal_angle_pass_ratio", normal_angle_pass_ratio)
        object.__setattr__(self, "density_sampling_metrics", density_sampling_metrics)
        object.__setattr__(self, "user_review_required", user_review_required)

    def to_mapping(self) -> dict[str, object]:
        """worker 결과 JSON으로 직렬화할 품질 사전을 반환합니다."""

        return {
            "status": self.status.value,
            "fitness": self.fitness,
            "rmse": self.rmse,
            "normalized_rmse_voxel": self.normalized_rmse_voxel,
            "normalized_rmse_diagonal": self.normalized_rmse_diagonal,
            "median_residual": self.median_residual,
            "p90_residual": self.p90_residual,
            "overlap_ratio": self.overlap_ratio,
            "reciprocal_ratio": self.reciprocal_ratio,
            "inlier_count": self.inlier_count,
            "query_count": self.query_count,
            "total_iterations": self.total_iterations,
            "delta_translation": self.delta_translation,
            "delta_rotation_degrees": self.delta_rotation_degrees,
            "marker_uniform_scale": self.marker_uniform_scale,
            "final_uniform_scale": self.final_uniform_scale,
            "scale_ratio": self.scale_ratio,
            "scale_refine_status": self.scale_refine_status.value,
            "scale_refine_reason": self.scale_refine_reason,
            "marker_drift_rms": self.marker_drift_rms,
            "elapsed_seconds": self.elapsed_seconds,
            "per_reference_model": [item.to_mapping() for item in self.per_reference_model],
            "review_reasons": list(self.review_reasons),
            "rejection_reasons": list(self.rejection_reasons),
            "baseline_metrics": (
                self.baseline_metrics.to_mapping() if self.baseline_metrics is not None else None
            ),
            "best_candidate_metrics": (
                self.best_candidate_metrics.to_mapping()
                if self.best_candidate_metrics is not None
                else None
            ),
            "relative_improvement": self.relative_improvement,
            "best_level_name": self.best_level_name,
            "best_iteration": self.best_iteration,
            "solver_stop_reason": self.solver_stop_reason.value,
            "point_to_plane_rmse": self.point_to_plane_rmse,
            "normal_valid_ratio": self.normal_valid_ratio,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
            "density_sampling_metrics": (
                self.density_sampling_metrics.to_mapping()
                if self.density_sampling_metrics is not None
                else None
            ),
            "user_review_required": self.user_review_required,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpQualityMetrics":
        """worker 결과 JSON에서 검증된 품질 지표를 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 품질 결과는 사전이어야 합니다.")
        try:
            return cls(
                status=IcpQualityStatus(str(value["status"])),
                fitness=_strict_finite_float(value["fitness"], "ICP 품질 fitness"),
                rmse=_strict_finite_float(value["rmse"], "ICP 품질 RMSE"),
                normalized_rmse_voxel=_strict_finite_float(value["normalized_rmse_voxel"], "ICP 품질 voxel 정규화 RMSE"),
                normalized_rmse_diagonal=_strict_finite_float(value["normalized_rmse_diagonal"], "ICP 품질 대각선 정규화 RMSE"),
                median_residual=_strict_finite_float(value["median_residual"], "ICP 품질 median residual"),
                p90_residual=_strict_finite_float(value["p90_residual"], "ICP 품질 p90 residual"),
                overlap_ratio=_strict_finite_float(value["overlap_ratio"], "ICP 품질 overlap 비율"),
                reciprocal_ratio=_strict_finite_float(value["reciprocal_ratio"], "ICP 품질 reciprocal 비율"),
                inlier_count=_strict_nonnegative_int(value["inlier_count"], "ICP 품질 inlier 수"),
                query_count=_strict_nonnegative_int(value["query_count"], "ICP 품질 query 수"),
                total_iterations=_strict_nonnegative_int(value["total_iterations"], "ICP 품질 총 반복 수"),
                delta_translation=_strict_finite_float(value["delta_translation"], "ICP 품질 이동량"),
                delta_rotation_degrees=_strict_finite_float(value["delta_rotation_degrees"], "ICP 품질 회전량"),
                marker_uniform_scale=_strict_finite_float(value["marker_uniform_scale"], "ICP 품질 marker 스케일"),
                final_uniform_scale=_strict_finite_float(value["final_uniform_scale"], "ICP 품질 최종 스케일"),
                scale_ratio=_strict_finite_float(value["scale_ratio"], "ICP 품질 스케일 비율"),
                scale_refine_status=IcpScaleRefineStatus(str(value["scale_refine_status"])),
                scale_refine_reason=str(value.get("scale_refine_reason", "")),
                marker_drift_rms=_strict_finite_float(value["marker_drift_rms"], "ICP 품질 marker drift"),
                elapsed_seconds=_strict_finite_float(value["elapsed_seconds"], "ICP 품질 경과 시간"),
                per_reference_model=tuple(
                    IcpPerReferenceMetrics.from_mapping(item)
                    for item in _strict_mapping_sequence(
                        value.get("per_reference_model", ()),
                        "ICP 기준 모델 품질 목록",
                    )
                ),
                review_reasons=tuple(
                    str(item)
                    for item in _strict_mapping_sequence(value.get("review_reasons", ()), "ICP 검토 사유")
                ),
                rejection_reasons=tuple(
                    str(item)
                    for item in _strict_mapping_sequence(value.get("rejection_reasons", ()), "ICP 거부 사유")
                ),
                baseline_metrics=(
                    IcpEvaluationMetrics.from_mapping(value["baseline_metrics"])
                    if value.get("baseline_metrics") is not None
                    else None
                ),
                best_candidate_metrics=(
                    IcpEvaluationMetrics.from_mapping(value["best_candidate_metrics"])
                    if value.get("best_candidate_metrics") is not None
                    else None
                ),
                relative_improvement=_strict_finite_float(
                    value.get("relative_improvement", 0.0),
                    "ICP 품질 상대 개선율",
                ),
                best_level_name=str(value.get("best_level_name", "")),
                best_iteration=_strict_nonnegative_int(
                    value.get("best_iteration", 0),
                    "ICP 품질 최선 반복",
                ),
                solver_stop_reason=IcpSolverStopReason(
                    str(value.get("solver_stop_reason", IcpSolverStopReason.NONE.value))
                ),
                point_to_plane_rmse=_strict_finite_float(
                    value.get("point_to_plane_rmse", 0.0),
                    "ICP 품질 plane RMSE",
                ),
                normal_valid_ratio=_strict_finite_float(
                    value.get("normal_valid_ratio", 1.0),
                    "ICP 품질 노멀 유효 비율",
                ),
                normal_angle_pass_ratio=_strict_finite_float(
                    value.get("normal_angle_pass_ratio", 1.0),
                    "ICP 품질 노멀 각도 통과 비율",
                ),
                density_sampling_metrics=(
                    IcpDensitySamplingMetrics.from_mapping(value["density_sampling_metrics"])
                    if value.get("density_sampling_metrics") is not None
                    else None
                ),
                user_review_required=_strict_exact_bool(
                    value.get("user_review_required", False),
                    "ICP 사용자 검토 필요 여부",
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 품질 결과 복원에 실패했습니다.") from error


@dataclass(frozen=True, slots=True)
class IcpAlignmentResult:
    """worker가 반환하는 최종 비파괴 ICP 정렬 결과입니다."""

    moving_model_id: str
    reference_model_ids: tuple[str, ...]
    initial_alignment: Sim3Transform
    final_alignment: Sim3Transform
    icp_delta: Sim3Transform
    quality: IcpQualityMetrics
    level_results: tuple[IcpLevelResult, ...]
    input_fingerprint: str
    parameter_fingerprint: str
    model_revision_snapshot: tuple[tuple[str, int], ...]
    marker_signature: str
    algorithm_version: str = ICP_ALGORITHM_VERSION
    best_candidate: IcpBestCandidate | None = None
    baseline_metrics: IcpEvaluationMetrics | None = None
    best_candidate_metrics: IcpEvaluationMetrics | None = None
    relative_improvement: float = 0.0
    best_level_name: str = ""
    best_iteration: int = 0
    solver_stop_reason: IcpSolverStopReason | str = IcpSolverStopReason.NONE
    point_to_plane_rmse: float = 0.0
    normal_valid_ratio: float = 1.0
    normal_angle_pass_ratio: float = 1.0
    density_sampling_metrics: IcpDensitySamplingMetrics | None = None
    user_review_required: bool = False

    def __post_init__(self) -> None:
        """결과 transform 체인·품질·입력 snapshot 식별자를 검증합니다."""

        moving_model_id = str(self.moving_model_id).strip()
        reference_model_ids = tuple(str(item).strip() for item in self.reference_model_ids)
        if not moving_model_id or not reference_model_ids or any(not item for item in reference_model_ids):
            raise IcpInputError("ICP 결과 모델 식별자가 올바르지 않습니다.")
        if len(set(reference_model_ids)) != len(reference_model_ids):
            raise IcpInputError("ICP 결과 기준 모델 ID가 중복됩니다.")
        if not all(isinstance(item, Sim3Transform) for item in (self.initial_alignment, self.final_alignment, self.icp_delta)):
            raise IcpInputError("ICP 결과 transform 형식이 올바르지 않습니다.")
        expected_final = self.icp_delta.matrix_float64 @ self.initial_alignment.matrix_float64
        if not np.allclose(
            expected_final,
            self.final_alignment.matrix_float64,
            rtol=0.0,
            atol=1.0e-8,
        ):
            raise IcpInputError("ICP 최종 alignment가 delta × initial 계약과 다릅니다.")
        if not isinstance(self.quality, IcpQualityMetrics):
            raise IcpInputError("ICP 결과 품질 형식이 올바르지 않습니다.")
        level_results = tuple(self.level_results)
        if not level_results or not all(isinstance(item, IcpLevelResult) for item in level_results):
            raise IcpInputError("ICP 레벨 결과 형식이 올바르지 않습니다.")
        input_fingerprint = str(self.input_fingerprint).strip()
        parameter_fingerprint = str(self.parameter_fingerprint).strip()
        if not input_fingerprint or not parameter_fingerprint:
            raise IcpInputError("ICP 결과 fingerprint가 비어 있습니다.")
        revisions = tuple(sorted(_strict_revision_snapshot(self.model_revision_snapshot, "ICP 결과 revision snapshot")))
        if not revisions or any(not model_id or revision < 0 for model_id, revision in revisions):
            raise IcpInputError("ICP 결과 revision snapshot이 올바르지 않습니다.")
        marker_signature = str(self.marker_signature).strip()
        algorithm_version = str(self.algorithm_version).strip()
        if not marker_signature or algorithm_version != ICP_ALGORITHM_VERSION:
            raise IcpInputError("ICP 결과 signature 또는 알고리즘 버전이 올바르지 않습니다.")
        per_reference = self.quality.per_reference_model
        if tuple(item.model_id for item in per_reference) != reference_model_ids:
            raise IcpInputError("ICP 결과 기준 모델 순서와 품질 기여도 순서가 다릅니다.")
        if sum(item.correspondence_count for item in per_reference) != self.quality.inlier_count:
            raise IcpInputError("ICP 결과 기준 모델 대응점 수 합이 inlier 수와 다릅니다.")
        best_candidate = self.best_candidate
        if best_candidate is not None:
            if not isinstance(best_candidate, IcpBestCandidate):
                raise IcpInputError("ICP 결과 최선 후보 형식이 올바르지 않습니다.")
            if not np.allclose(
                best_candidate.final_alignment.matrix_float64,
                self.final_alignment.matrix_float64,
                rtol=0.0,
                atol=1.0e-8,
            ):
                raise IcpInputError("ICP 결과 최선 후보와 최종 alignment가 다릅니다.")
        baseline_metrics = self.baseline_metrics or self.quality.baseline_metrics
        best_candidate_metrics = self.best_candidate_metrics or self.quality.best_candidate_metrics
        if (baseline_metrics is None) != (best_candidate_metrics is None):
            raise IcpInputError("ICP 결과 baseline과 candidate 지표는 함께 제공되어야 합니다.")
        if baseline_metrics is not None and not isinstance(baseline_metrics, IcpEvaluationMetrics):
            raise IcpInputError("ICP 결과 baseline 지표 형식이 올바르지 않습니다.")
        if best_candidate_metrics is not None and not isinstance(best_candidate_metrics, IcpEvaluationMetrics):
            raise IcpInputError("ICP 결과 candidate 지표 형식이 올바르지 않습니다.")
        relative_improvement = _strict_finite_float(
            self.relative_improvement if self.baseline_metrics is not None else self.quality.relative_improvement,
            "ICP 결과 상대 개선율",
        )
        best_level_name = str(
            self.best_level_name if self.best_level_name else self.quality.best_level_name
        ).strip().lower()
        best_iteration = _strict_nonnegative_int(
            self.best_iteration if self.best_iteration else self.quality.best_iteration,
            "ICP 결과 최선 반복",
        )
        try:
            solver_stop_reason = (
                self.solver_stop_reason
                if self.solver_stop_reason is not IcpSolverStopReason.NONE
                else self.quality.solver_stop_reason
            )
            if not isinstance(solver_stop_reason, IcpSolverStopReason):
                solver_stop_reason = IcpSolverStopReason(str(solver_stop_reason))
        except ValueError as error:
            raise IcpInputError("ICP 결과 solver 종료 사유가 올바르지 않습니다.") from error
        point_to_plane_rmse = _strict_finite_float(
            self.point_to_plane_rmse if self.point_to_plane_rmse else self.quality.point_to_plane_rmse,
            "ICP 결과 plane RMSE",
        )
        normal_valid_ratio = _strict_finite_float(
            self.normal_valid_ratio if self.normal_valid_ratio != 1.0 else self.quality.normal_valid_ratio,
            "ICP 결과 노멀 유효 비율",
        )
        normal_angle_pass_ratio = _strict_finite_float(
            self.normal_angle_pass_ratio
            if self.normal_angle_pass_ratio != 1.0
            else self.quality.normal_angle_pass_ratio,
            "ICP 결과 노멀 각도 통과 비율",
        )
        density_sampling_metrics = self.density_sampling_metrics or self.quality.density_sampling_metrics
        if density_sampling_metrics is not None and not isinstance(density_sampling_metrics, IcpDensitySamplingMetrics):
            raise IcpInputError("ICP 결과 밀도 표본 지표 형식이 올바르지 않습니다.")
        user_review_required = _strict_exact_bool(
            self.user_review_required or self.quality.user_review_required,
            "ICP 결과 사용자 검토 필요 여부",
        )
        if point_to_plane_rmse < 0.0 or not 0.0 <= normal_valid_ratio <= 1.0 or not 0.0 <= normal_angle_pass_ratio <= 1.0:
            raise IcpInputError("ICP 결과 노멀·plane 지표 범위가 올바르지 않습니다.")
        object.__setattr__(self, "moving_model_id", moving_model_id)
        object.__setattr__(self, "reference_model_ids", reference_model_ids)
        object.__setattr__(self, "level_results", level_results)
        object.__setattr__(self, "input_fingerprint", input_fingerprint)
        object.__setattr__(self, "parameter_fingerprint", parameter_fingerprint)
        object.__setattr__(self, "model_revision_snapshot", revisions)
        object.__setattr__(self, "marker_signature", marker_signature)
        object.__setattr__(self, "algorithm_version", algorithm_version)
        object.__setattr__(self, "best_candidate", best_candidate)
        object.__setattr__(self, "baseline_metrics", baseline_metrics)
        object.__setattr__(self, "best_candidate_metrics", best_candidate_metrics)
        object.__setattr__(self, "relative_improvement", relative_improvement)
        object.__setattr__(self, "best_level_name", best_level_name)
        object.__setattr__(self, "best_iteration", best_iteration)
        object.__setattr__(self, "solver_stop_reason", solver_stop_reason)
        object.__setattr__(self, "point_to_plane_rmse", point_to_plane_rmse)
        object.__setattr__(self, "normal_valid_ratio", normal_valid_ratio)
        object.__setattr__(self, "normal_angle_pass_ratio", normal_angle_pass_ratio)
        object.__setattr__(self, "density_sampling_metrics", density_sampling_metrics)
        object.__setattr__(self, "user_review_required", user_review_required)

    @property
    def initial_alignment_float64(self) -> np.ndarray:
        """worker·runtime 호환을 위해 초기 4×4 행렬을 반환합니다."""

        return self.initial_alignment.matrix_float64

    @property
    def final_alignment_float64(self) -> np.ndarray:
        """worker·runtime 호환을 위해 최종 4×4 행렬을 반환합니다."""

        return self.final_alignment.matrix_float64

    @property
    def icp_delta_float64(self) -> np.ndarray:
        """worker·runtime 호환을 위해 ICP delta 4×4 행렬을 반환합니다."""

        return self.icp_delta.matrix_float64

    def to_mapping(self) -> dict[str, object]:
        """result.json에 쓸 최종 ICP 결과의 JSON 안전 사전을 반환합니다."""

        return {
            "moving_model_id": self.moving_model_id,
            "reference_model_ids": list(self.reference_model_ids),
            "initial_alignment_4x4": self.initial_alignment.matrix_float64.tolist(),
            "final_alignment_4x4": self.final_alignment.matrix_float64.tolist(),
            "icp_delta_4x4": self.icp_delta.matrix_float64.tolist(),
            "quality": self.quality.to_mapping(),
            "level_results": [item.to_mapping() for item in self.level_results],
            "input_fingerprint": self.input_fingerprint,
            "parameter_fingerprint": self.parameter_fingerprint,
            "model_revision_snapshot": [list(item) for item in self.model_revision_snapshot],
            "marker_signature": self.marker_signature,
            "algorithm_version": self.algorithm_version,
            "best_candidate": (
                self.best_candidate.to_mapping() if self.best_candidate is not None else None
            ),
            "baseline_metrics": (
                self.baseline_metrics.to_mapping() if self.baseline_metrics is not None else None
            ),
            "best_candidate_metrics": (
                self.best_candidate_metrics.to_mapping()
                if self.best_candidate_metrics is not None
                else None
            ),
            "relative_improvement": self.relative_improvement,
            "best_level_name": self.best_level_name,
            "best_iteration": self.best_iteration,
            "solver_stop_reason": self.solver_stop_reason.value,
            "point_to_plane_rmse": self.point_to_plane_rmse,
            "normal_valid_ratio": self.normal_valid_ratio,
            "normal_angle_pass_ratio": self.normal_angle_pass_ratio,
            "density_sampling_metrics": (
                self.density_sampling_metrics.to_mapping()
                if self.density_sampling_metrics is not None
                else None
            ),
            "user_review_required": self.user_review_required,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IcpAlignmentResult":
        """worker result.json에서 검증된 최종 ICP 결과를 복원합니다."""

        if not isinstance(value, Mapping):
            raise IcpWorkerError("ICP 결과는 사전이어야 합니다.")
        try:
            return cls(
                moving_model_id=str(value["moving_model_id"]),
                reference_model_ids=tuple(
                    str(item)
                    for item in _strict_mapping_sequence(value["reference_model_ids"], "ICP 결과 기준 모델 ID")
                ),
                initial_alignment=Sim3Transform(matrix_float64=value["initial_alignment_4x4"]),
                final_alignment=Sim3Transform(matrix_float64=value["final_alignment_4x4"]),
                icp_delta=Sim3Transform(matrix_float64=value["icp_delta_4x4"]),
                quality=IcpQualityMetrics.from_mapping(value["quality"]),
                level_results=tuple(
                    IcpLevelResult.from_mapping(item)
                    for item in _strict_mapping_sequence(value.get("level_results", ()), "ICP 레벨 결과")
                ),
                input_fingerprint=str(value["input_fingerprint"]),
                parameter_fingerprint=str(value["parameter_fingerprint"]),
                model_revision_snapshot=_strict_revision_snapshot(
                    value.get("model_revision_snapshot", ()),
                    "ICP 결과 revision snapshot",
                ),
                marker_signature=str(value["marker_signature"]),
                algorithm_version=str(value.get("algorithm_version", ICP_ALGORITHM_VERSION)),
                best_candidate=(
                    IcpBestCandidate.from_mapping(value["best_candidate"])
                    if value.get("best_candidate") is not None
                    else None
                ),
                baseline_metrics=(
                    IcpEvaluationMetrics.from_mapping(value["baseline_metrics"])
                    if value.get("baseline_metrics") is not None
                    else None
                ),
                best_candidate_metrics=(
                    IcpEvaluationMetrics.from_mapping(value["best_candidate_metrics"])
                    if value.get("best_candidate_metrics") is not None
                    else None
                ),
                relative_improvement=_strict_finite_float(
                    value.get("relative_improvement", 0.0),
                    "ICP 결과 상대 개선율",
                ),
                best_level_name=str(value.get("best_level_name", "")),
                best_iteration=_strict_nonnegative_int(
                    value.get("best_iteration", 0),
                    "ICP 결과 최선 반복",
                ),
                solver_stop_reason=IcpSolverStopReason(
                    str(value.get("solver_stop_reason", IcpSolverStopReason.NONE.value))
                ),
                point_to_plane_rmse=_strict_finite_float(
                    value.get("point_to_plane_rmse", 0.0),
                    "ICP 결과 plane RMSE",
                ),
                normal_valid_ratio=_strict_finite_float(
                    value.get("normal_valid_ratio", 1.0),
                    "ICP 결과 노멀 유효 비율",
                ),
                normal_angle_pass_ratio=_strict_finite_float(
                    value.get("normal_angle_pass_ratio", 1.0),
                    "ICP 결과 노멀 각도 통과 비율",
                ),
                density_sampling_metrics=(
                    IcpDensitySamplingMetrics.from_mapping(value["density_sampling_metrics"])
                    if value.get("density_sampling_metrics") is not None
                    else None
                ),
                user_review_required=_strict_exact_bool(
                    value.get("user_review_required", False),
                    "ICP 결과 사용자 검토 필요 여부",
                ),
            )
        except (KeyError, TypeError, ValueError, IndexError, OverflowError, IcpInputError) as error:
            raise IcpWorkerError("ICP 결과 복원에 실패했습니다.") from error


__all__ = (
    "ICP_ALGORITHM_VERSION",
    "IcpAlignmentResult",
    "IcpBestCandidate",
    "IcpCancelledError",
    "IcpDensitySamplingMetrics",
    "IcpDegeneracyError",
    "IcpError",
    "IcpEvaluationMetrics",
    "IcpInputError",
    "IcpInputSnapshot",
    "IcpIterationMetrics",
    "IcpLevelParameters",
    "IcpLevelResult",
    "IcpMovingCloudFrame",
    "IcpNormalCoverageError",
    "IcpObjectiveKind",
    "IcpNumericalError",
    "IcpOverlapError",
    "IcpParameters",
    "IcpPerReferenceMetrics",
    "IcpPointCloud",
    "IcpPreset",
    "IcpProgressSnapshot",
    "IcpQualityMetrics",
    "IcpQualityRejectedError",
    "IcpQualityStatus",
    "IcpRobustKernelKind",
    "IcpScaleMode",
    "IcpScaleRefineStatus",
    "IcpStaleResultError",
    "IcpSolverStopReason",
    "IcpWorkerError",
    "stream_array_fingerprint",
)
