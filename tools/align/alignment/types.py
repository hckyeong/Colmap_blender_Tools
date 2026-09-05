"""마커 기반 Sim(3) 정렬에서 공유하는 불변 데이터 타입을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from typing import Any, Final

import numpy as np


_IDENTITY_4X4: Final[np.ndarray] = np.identity(4, dtype=np.float64)
_MATRIX_TOLERANCE: Final[float] = 1.0e-10
_ALIGNMENT_PROVENANCE_METHODS: Final[frozenset[str]] = frozenset(
    {"ANCHOR", "MANUAL", "MARKER", "ICP", "UNKNOWN"}
)


def _utc_timestamp() -> str:
    """감사 기록에 사용할 UTC ISO-8601 시각을 생성합니다."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")



def _normalize_marker_geometry_dependencies(values: object) -> tuple[tuple[str, str], ...]:
    """감사 기록과 계산 초기값의 마커 형상 종속성을 정렬된 불변 쌍으로 검증합니다."""
    from ..constants import MARKER_GEOMETRY_SOURCES

    dependencies = set()
    for model_id, source in values:
        model_id = str(model_id).strip()
        source = str(source).strip().upper()
        if not model_id or source not in MARKER_GEOMETRY_SOURCES:
            raise ValueError("마커 형상 종속성의 소유자 또는 출처가 올바르지 않습니다.")
        dependencies.add((model_id, source))
    return tuple(sorted(dependencies))


@dataclass(frozen=True, slots=True)
class AlignmentProvenanceRecord:
    """최종 정렬의 출처와 품질 지표만 보관하는 작은 불변 감사 기록입니다."""

    model_id: str
    method: str
    backend_id: str = ""
    algorithm_version: str = ""
    input_fingerprint: str = ""
    result_fingerprint: str = ""
    marker_rms: float | None = None
    marker_maximum_error: float | None = None
    icp_fitness: float | None = None
    icp_rmse: float | None = None
    icp_status: str | None = None
    committed_at_utc: str = ""
    marker_geometry_dependencies: tuple[tuple[str, str], ...] = ()
    solver_geometry_dependencies: tuple[tuple[str, str], ...] = ()
    engine_metadata_json: str = ""

    def __post_init__(self) -> None:
        """감사 기록의 품질·마커/정밀 입력 출처·엔진 메타데이터를 검증합니다."""

        model_id = str(self.model_id).strip()
        method = str(self.method).strip().upper()
        if not model_id:
            raise ValueError("정렬 provenance 모델 ID가 비어 있습니다.")
        if method not in _ALIGNMENT_PROVENANCE_METHODS:
            raise ValueError("정렬 provenance 방법이 올바르지 않습니다.")
        backend_id = str(self.backend_id).strip()
        algorithm_version = str(self.algorithm_version).strip()
        input_fingerprint = str(self.input_fingerprint).strip()
        result_fingerprint = str(self.result_fingerprint).strip()
        icp_status = (
            None
            if self.icp_status is None
            else str(self.icp_status).strip().upper() or None
        )
        committed_at_utc = str(self.committed_at_utc).strip() or _utc_timestamp()
        try:
            datetime.fromisoformat(committed_at_utc.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("정렬 provenance 확정 시각이 ISO-8601 형식이 아닙니다.") from error

        numeric_values = {
            "marker_rms": self.marker_rms,
            "marker_maximum_error": self.marker_maximum_error,
            "icp_fitness": self.icp_fitness,
            "icp_rmse": self.icp_rmse,
        }
        normalized_values: dict[str, float | None] = {}
        for field_name, raw_value in numeric_values.items():
            if raw_value is None:
                normalized_values[field_name] = None
                continue
            value = float(raw_value)
            if not isfinite(value) or value < 0.0:
                raise ValueError(f"정렬 provenance {field_name} 값이 올바르지 않습니다.")
            normalized_values[field_name] = value
        fitness = normalized_values["icp_fitness"]
        if fitness is not None and fitness > 1.0:
            raise ValueError("정렬 provenance icp_fitness는 0과 1 사이여야 합니다.")

        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "backend_id", backend_id)
        object.__setattr__(self, "algorithm_version", algorithm_version)
        object.__setattr__(self, "input_fingerprint", input_fingerprint)
        object.__setattr__(self, "result_fingerprint", result_fingerprint)
        object.__setattr__(self, "marker_rms", normalized_values["marker_rms"])
        object.__setattr__(
            self,
            "marker_maximum_error",
            normalized_values["marker_maximum_error"],
        )
        object.__setattr__(self, "icp_fitness", fitness)
        object.__setattr__(self, "icp_rmse", normalized_values["icp_rmse"])
        object.__setattr__(self, "icp_status", icp_status)
        object.__setattr__(self, "committed_at_utc", committed_at_utc)
        object.__setattr__(
            self, "marker_geometry_dependencies",
            _normalize_marker_geometry_dependencies(self.marker_geometry_dependencies),
        )
        object.__setattr__(self, "solver_geometry_dependencies",
                           _normalize_marker_geometry_dependencies(self.solver_geometry_dependencies))
        metadata = str(self.engine_metadata_json)
        if metadata:
            if len(metadata.encode("utf-8")) > 262144:
                raise ValueError("정렬 엔진 출처 정보가 256 KiB 제한을 초과했습니다.")
            decoded = json.loads(metadata)
            if not isinstance(decoded, dict):
                raise ValueError("정렬 엔진 출처 정보는 JSON 객체여야 합니다.")
            metadata = json.dumps(decoded, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False)
        object.__setattr__(self, "engine_metadata_json", metadata)

    @property
    def geometry_dependencies(self) -> tuple[tuple[str, str], ...]:
        """축 변경 시 무효화할 마커와 정밀 정렬의 형상 출처를 합칩니다."""
        return tuple(sorted(set(self.marker_geometry_dependencies + self.solver_geometry_dependencies)))

    def to_mapping(self) -> dict[str, Any]:
        """기존 감사 필드와 실제 사용한 형상·엔진 출처를 저장 값으로 반환합니다."""

        return {
            "model_id": self.model_id,
            "method": self.method,
            "backend_id": self.backend_id,
            "algorithm_version": self.algorithm_version,
            "input_fingerprint": self.input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "marker_rms": self.marker_rms,
            "marker_maximum_error": self.marker_maximum_error,
            "icp_fitness": self.icp_fitness,
            "icp_rmse": self.icp_rmse,
            "icp_status": self.icp_status,
            "committed_at_utc": self.committed_at_utc,
            **(
                {"marker_geometry_dependencies": [list(item) for item in self.marker_geometry_dependencies]}
                if self.marker_geometry_dependencies else {}
            ),
            **({"solver_geometry_dependencies": [list(item) for item in self.solver_geometry_dependencies]}
               if self.solver_geometry_dependencies else {}),
            **({"engine_metadata": json.loads(self.engine_metadata_json)}
               if self.engine_metadata_json else {}),
        }


class MarkerAlignmentError(RuntimeError):
    """마커 정렬 경계에서 호출자에게 전달할 기본 오류입니다."""


class MarkerInputError(MarkerAlignmentError):
    """마커·모델 snapshot이 정렬 조건을 만족하지 않을 때 발생합니다."""


class MarkerDegeneracyError(MarkerAlignmentError):
    """마커 분포가 공선성 등으로 Sim(3)을 안정적으로 결정하지 못할 때 발생합니다."""


class MarkerNumericalError(MarkerAlignmentError):
    """SVD·역행렬 등 수치 계산 결과가 유효하지 않을 때 발생합니다."""


class MarkerPreviewStaleError(MarkerAlignmentError):
    """미리보기 생성 뒤 마커 또는 모델 입력이 달라졌을 때 발생합니다."""


class IcpBackendUnavailableError(MarkerAlignmentError):
    """실제 ICP 실행기가 등록되지 않은 상태에서 실행을 요청할 때 발생합니다."""


def _freeze_float64_array(
    value: object,
    *,
    field_name: str,
    shape: tuple[int | None, ...],
) -> np.ndarray:
    """유한한 float64 배열을 검증하고 외부 변경을 막는 읽기 전용 배열로 고정합니다."""

    # 호출자 배열과 alias를 공유하지 않아 외부 입력의 writeable 상태를 바꾸지 않습니다.
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


def _fingerprint_matrix(matrix_float64: np.ndarray) -> str:
    """행렬 값의 결정적 바이트 표현으로 짧은 지문을 생성합니다."""

    digest = hashlib.blake2b(digest_size=16)
    digest.update(np.ascontiguousarray(matrix_float64, dtype=np.dtype("<f8")).tobytes())
    return digest.hexdigest()


def _validate_affine_sim3_matrix(matrix: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """4×4 column-vector Sim(3) 행렬에서 scale·회전·이동을 분리해 검증합니다."""

    if not np.allclose(matrix[3], (0.0, 0.0, 0.0, 1.0), rtol=0.0, atol=_MATRIX_TOLERANCE):
        raise ValueError("Sim(3) 행렬의 마지막 행은 (0, 0, 0, 1)이어야 합니다.")
    linear = matrix[:3, :3]
    determinant = float(np.linalg.det(linear))
    if not isfinite(determinant) or determinant <= 0.0:
        raise ValueError("Sim(3) 선형 행렬식은 유한한 양수여야 합니다.")
    scale = float(np.cbrt(determinant))
    if not isfinite(scale) or scale <= 0.0:
        raise ValueError("Sim(3) 균일 스케일은 유한한 양수여야 합니다.")
    rotation = linear / scale
    if not np.allclose(
        rotation.T @ rotation,
        np.identity(3, dtype=np.float64),
        rtol=0.0,
        atol=_MATRIX_TOLERANCE,
    ):
        raise ValueError("Sim(3) 선형 행렬은 균일 스케일과 회전만 포함해야 합니다.")
    rotation_determinant = float(np.linalg.det(rotation))
    if not np.isclose(rotation_determinant, 1.0, rtol=0.0, atol=_MATRIX_TOLERANCE):
        raise ValueError("Sim(3) 회전 행렬식은 +1이어야 합니다.")
    return scale, rotation, matrix[:3, 3]


@dataclass(frozen=True, slots=True)
class Sim3Transform:
    """column-vector 규약의 양의 균일 스케일·회전·이동 불변 변환입니다."""

    matrix_float64: np.ndarray
    inverse_float64: np.ndarray | None = None
    rotation_float64: np.ndarray | None = None
    translation_float64: np.ndarray | None = None
    uniform_scale: float | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        """행렬과 분해값을 값 크기에 맞는 허용오차로 검증해 읽기 전용 Sim(3)으로 고정합니다."""

        matrix = _freeze_float64_array(
            self.matrix_float64,
            field_name="Sim(3) 행렬",
            shape=(4, 4),
        )
        computed_scale, computed_rotation, computed_translation = _validate_affine_sim3_matrix(
            matrix
        )
        requested_scale = self.uniform_scale
        if requested_scale is not None and not np.isclose(
            float(requested_scale),
            computed_scale,
            rtol=_MATRIX_TOLERANCE,
            atol=_MATRIX_TOLERANCE,
        ):
            raise ValueError("Sim(3) uniform_scale 값이 행렬과 일치하지 않습니다.")
        requested_rotation = self.rotation_float64
        if requested_rotation is not None and not np.allclose(
            _freeze_float64_array(
                requested_rotation,
                field_name="Sim(3) 회전",
                shape=(3, 3),
            ),
            computed_rotation,
            rtol=0.0,
            atol=_MATRIX_TOLERANCE,
        ):
            raise ValueError("Sim(3) rotation 값이 행렬과 일치하지 않습니다.")
        requested_translation = self.translation_float64
        if requested_translation is not None and not np.allclose(
            _freeze_float64_array(
                requested_translation,
                field_name="Sim(3) 이동",
                shape=(3,),
            ),
            computed_translation,
            rtol=0.0,
            atol=_MATRIX_TOLERANCE,
        ):
            raise ValueError("Sim(3) translation 값이 행렬과 일치하지 않습니다.")

        inverse_value = self.inverse_float64
        inverse = (
            np.linalg.inv(matrix)
            if inverse_value is None
            else _freeze_float64_array(
                inverse_value,
                field_name="Sim(3) 역행렬",
                shape=(4, 4),
            )
        )
        inverse = _freeze_float64_array(
            inverse,
            field_name="Sim(3) 역행렬",
            shape=(4, 4),
        )
        if not np.allclose(matrix @ inverse, _IDENTITY_4X4, rtol=0.0, atol=1.0e-9):
            raise ValueError("Sim(3) 행렬과 역행렬의 곱이 Identity가 아닙니다.")

        fingerprint = _fingerprint_matrix(matrix)
        if self.fingerprint and self.fingerprint != fingerprint:
            raise ValueError("Sim(3) fingerprint가 행렬 값과 일치하지 않습니다.")
        computed_rotation = np.ascontiguousarray(computed_rotation, dtype=np.float64)
        computed_rotation.setflags(write=False)
        computed_translation = np.ascontiguousarray(computed_translation, dtype=np.float64)
        computed_translation.setflags(write=False)
        object.__setattr__(self, "matrix_float64", matrix)
        object.__setattr__(self, "inverse_float64", inverse)
        object.__setattr__(self, "rotation_float64", computed_rotation)
        object.__setattr__(self, "translation_float64", computed_translation)
        object.__setattr__(self, "uniform_scale", computed_scale)
        object.__setattr__(self, "fingerprint", fingerprint)


def create_sim3_transform(
    rotation_float64: object,
    uniform_scale: float,
    translation_float64: object,
) -> Sim3Transform:
    """회전·양의 스케일·이동에서 column-vector 4×4 Sim(3)을 생성합니다."""

    rotation = _freeze_float64_array(
        rotation_float64,
        field_name="Sim(3) 회전",
        shape=(3, 3),
    )
    translation = _freeze_float64_array(
        translation_float64,
        field_name="Sim(3) 이동",
        shape=(3,),
    )
    scale = float(uniform_scale)
    if not isfinite(scale) or scale <= 0.0:
        raise ValueError("Sim(3) 균일 스케일은 유한한 양수여야 합니다.")
    if not np.allclose(
        rotation.T @ rotation,
        np.identity(3, dtype=np.float64),
        rtol=0.0,
        atol=_MATRIX_TOLERANCE,
    ):
        raise ValueError("Sim(3) 회전은 직교 행렬이어야 합니다.")
    if not np.isclose(float(np.linalg.det(rotation)), 1.0, rtol=0.0, atol=_MATRIX_TOLERANCE):
        raise ValueError("Sim(3) 회전 행렬식은 +1이어야 합니다.")
    matrix = np.identity(4, dtype=np.float64)
    matrix[:3, :3] = scale * rotation
    matrix[:3, 3] = translation
    return Sim3Transform(
        matrix_float64=matrix,
        rotation_float64=rotation,
        translation_float64=translation,
        uniform_scale=scale,
    )


def identity_sim3_transform() -> Sim3Transform:
    """모델 등록과 테스트에서 명시적으로 사용할 Identity Sim(3)을 생성합니다."""

    return Sim3Transform(matrix_float64=_IDENTITY_4X4)


def compose_sim3_transforms(left: Sim3Transform, right: Sim3Transform) -> Sim3Transform:
    """column-vector 규약으로 left × right 순서의 Sim(3) 합성 결과를 반환합니다."""

    if not isinstance(left, Sim3Transform) or not isinstance(right, Sim3Transform):
        raise TypeError("Sim(3) 합성에는 Sim3Transform 두 개가 필요합니다.")
    return Sim3Transform(matrix_float64=left.matrix_float64 @ right.matrix_float64)


def transform_positions(transform: Sim3Transform, positions: object) -> np.ndarray:
    """N×3 행 배열에 Sim(3)을 적용하고 유한한 float64 새 배열을 반환합니다."""

    if not isinstance(transform, Sim3Transform):
        raise TypeError("점 변환에는 Sim3Transform이 필요합니다.")
    source = _freeze_float64_array(
        positions,
        field_name="변환할 점 좌표",
        shape=(None, 3),
    )
    transformed = source @ transform.matrix_float64[:3, :3].T
    transformed = transformed + transform.translation_float64[np.newaxis, :]
    if not np.isfinite(transformed).all():
        raise MarkerNumericalError("Sim(3) 점 변환 결과에 유한하지 않은 값이 있습니다.")
    return np.ascontiguousarray(transformed, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class MarkerAlignmentInput:
    """마커 snapshot을 Project World 좌표로 고정한 순수 Sim(3) 입력입니다."""

    moving_model_id: str
    pair_ids: tuple[int, ...]
    reference_project: np.ndarray
    moving_project: np.ndarray
    marker_signature: str
    source_revision_snapshot: tuple[tuple[str, int], ...]
    reference_model_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """입력 길이·식별자·배열을 검증해 solver가 변경할 수 없게 고정합니다."""

        moving_model_id = str(self.moving_model_id).strip()
        if not moving_model_id:
            raise ValueError("이동 모델 ID가 비어 있습니다.")
        pair_ids = tuple(int(pair_id) for pair_id in self.pair_ids)
        if len(pair_ids) < 3 or any(pair_id <= 0 for pair_id in pair_ids):
            raise ValueError("마커 정렬에는 3개 이상의 양수 pair_id가 필요합니다.")
        if len(set(pair_ids)) != len(pair_ids):
            raise ValueError("마커 정렬 pair_id는 중복될 수 없습니다.")
        reference = _freeze_float64_array(
            self.reference_project,
            field_name="A Project World 마커",
            shape=(len(pair_ids), 3),
        )
        moving = _freeze_float64_array(
            self.moving_project,
            field_name="P Project World 마커",
            shape=(len(pair_ids), 3),
        )
        signature = str(self.marker_signature).strip()
        if not signature:
            raise ValueError("마커 입력 signature가 비어 있습니다.")
        revisions = tuple(
            sorted((str(model_id).strip(), int(revision)) for model_id, revision in self.source_revision_snapshot)
        )
        if not revisions or any(not model_id or revision < 0 for model_id, revision in revisions):
            raise ValueError("마커 입력 모델 revision snapshot이 올바르지 않습니다.")
        if len({model_id for model_id, _revision in revisions}) != len(revisions):
            raise ValueError("마커 입력 모델 revision snapshot에 중복 모델이 있습니다.")
        reference_model_ids = tuple(str(model_id).strip() for model_id in self.reference_model_ids)
        if any(not model_id for model_id in reference_model_ids):
            raise ValueError("A 모델 ID에 빈 값이 있습니다.")
        if len(set(reference_model_ids)) != len(reference_model_ids):
            raise ValueError("A 모델 ID는 중복될 수 없습니다.")
        object.__setattr__(self, "moving_model_id", moving_model_id)
        object.__setattr__(self, "pair_ids", pair_ids)
        object.__setattr__(self, "reference_project", reference)
        object.__setattr__(self, "moving_project", moving)
        object.__setattr__(self, "marker_signature", signature)
        object.__setattr__(self, "source_revision_snapshot", revisions)
        object.__setattr__(self, "reference_model_ids", reference_model_ids)


@dataclass(frozen=True, slots=True)
class MarkerResidualMetrics:
    """모든 유효 마커의 거리 잔차와 정규화된 품질 지표를 보관합니다."""

    count: int
    rms: float
    median: float
    maximum: float
    normalized_rms: float
    residuals_float64: np.ndarray
    uniform_scale: float
    rotation_determinant: float

    def __post_init__(self) -> None:
        """잔차 배열과 숫자 지표의 유한성·범위를 검증합니다."""

        count = int(self.count)
        if count < 3:
            raise ValueError("마커 잔차에는 3개 이상의 대응점이 필요합니다.")
        residuals = _freeze_float64_array(
            self.residuals_float64,
            field_name="마커 잔차",
            shape=(count,),
        )
        if np.any(residuals < 0.0):
            raise ValueError("마커 잔차는 음수일 수 없습니다.")
        values = {
            "RMS": float(self.rms),
            "중앙값": float(self.median),
            "최대 잔차": float(self.maximum),
            "정규화 RMS": float(self.normalized_rms),
            "균일 스케일": float(self.uniform_scale),
            "회전 행렬식": float(self.rotation_determinant),
        }
        if not all(isfinite(value) for value in values.values()):
            raise ValueError("마커 잔차 지표에 유한하지 않은 값이 있습니다.")
        if any(values[name] < 0.0 for name in ("RMS", "중앙값", "최대 잔차", "정규화 RMS")):
            raise ValueError("마커 잔차 지표는 음수일 수 없습니다.")
        if values["균일 스케일"] <= 0.0:
            raise ValueError("마커 잔차의 균일 스케일은 양수여야 합니다.")
        object.__setattr__(self, "count", count)
        object.__setattr__(self, "residuals_float64", residuals)
        for field_name, value in values.items():
            attribute = {
                "RMS": "rms",
                "중앙값": "median",
                "최대 잔차": "maximum",
                "정규화 RMS": "normalized_rms",
                "균일 스케일": "uniform_scale",
                "회전 행렬식": "rotation_determinant",
            }[field_name]
            object.__setattr__(self, attribute, value)


@dataclass(frozen=True, slots=True)
class RepresentativeTriangle:
    """표시 전용으로 고른 세 대응 마커의 원본 Project World 좌표를 보관합니다."""

    pair_ids: tuple[int, int, int]
    input_indices: tuple[int, int, int]
    reference_vertices: np.ndarray
    moving_vertices: np.ndarray

    def __post_init__(self) -> None:
        """세 개의 서로 다른 마커와 A/P 정점을 읽기 전용 배열로 고정합니다."""

        pair_ids = tuple(int(pair_id) for pair_id in self.pair_ids)
        input_indices = tuple(int(index) for index in self.input_indices)
        if len(pair_ids) != 3 or len(input_indices) != 3:
            raise ValueError("대표 삼각형은 정확히 세 개의 마커가 필요합니다.")
        if any(pair_id <= 0 for pair_id in pair_ids) or len(set(pair_ids)) != 3:
            raise ValueError("대표 삼각형 pair_id는 서로 다른 양수여야 합니다.")
        if any(index < 0 for index in input_indices) or len(set(input_indices)) != 3:
            raise ValueError("대표 삼각형 입력 인덱스는 서로 달라야 합니다.")
        object.__setattr__(self, "pair_ids", pair_ids)
        object.__setattr__(self, "input_indices", input_indices)
        object.__setattr__(
            self,
            "reference_vertices",
            _freeze_float64_array(
                self.reference_vertices,
                field_name="대표 A 삼각형 정점",
                shape=(3, 3),
            ),
        )
        object.__setattr__(
            self,
            "moving_vertices",
            _freeze_float64_array(
                self.moving_vertices,
                field_name="대표 P 삼각형 정점",
                shape=(3, 3),
            ),
        )


@dataclass(frozen=True, slots=True)
class MarkerAlignmentResult:
    """한 번의 마커 미리보기에 필요한 Sim(3) 결과·품질·시각화 입력을 보관합니다."""

    moving_model_id: str
    base_alignment: Sim3Transform
    delta_transform: Sim3Transform
    preview_alignment: Sim3Transform
    metrics: MarkerResidualMetrics
    representative_triangle: RepresentativeTriangle
    marker_signature: str
    source_revision_snapshot: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        """결과의 transform 체인과 snapshot 식별자를 검증합니다."""

        moving_model_id = str(self.moving_model_id).strip()
        if not moving_model_id:
            raise ValueError("마커 결과 이동 모델 ID가 비어 있습니다.")
        if not all(
            isinstance(value, Sim3Transform)
            for value in (self.base_alignment, self.delta_transform, self.preview_alignment)
        ):
            raise TypeError("마커 결과 transform은 모두 Sim3Transform이어야 합니다.")
        expected_preview = self.delta_transform.matrix_float64 @ self.base_alignment.matrix_float64
        if not np.allclose(
            self.preview_alignment.matrix_float64,
            expected_preview,
            rtol=0.0,
            atol=1.0e-9,
        ):
            raise ValueError("마커 미리보기 transform은 delta × 확정 transform이어야 합니다.")
        if not isinstance(self.metrics, MarkerResidualMetrics):
            raise TypeError("마커 결과 metrics 형식이 올바르지 않습니다.")
        if not isinstance(self.representative_triangle, RepresentativeTriangle):
            raise TypeError("마커 결과 대표 삼각형 형식이 올바르지 않습니다.")
        signature = str(self.marker_signature).strip()
        if not signature:
            raise ValueError("마커 결과 signature가 비어 있습니다.")
        revisions = tuple((str(model_id).strip(), int(revision)) for model_id, revision in self.source_revision_snapshot)
        if not revisions:
            raise ValueError("마커 결과 revision snapshot이 비어 있습니다.")
        object.__setattr__(self, "moving_model_id", moving_model_id)
        object.__setattr__(self, "marker_signature", signature)
        object.__setattr__(self, "source_revision_snapshot", revisions)


@dataclass(frozen=True, slots=True)
class IcpAlignmentSeed:
    """후속 ICP가 원본을 바꾸지 않고 사용할 정렬 초기값 snapshot입니다."""

    moving_model_id: str
    reference_model_ids: tuple[str, ...]
    initial_alignment_float64: np.ndarray
    base_committed_alignment_float64: np.ndarray
    marker_result: MarkerAlignmentResult | None
    model_revision_snapshot: tuple[tuple[str, int], ...]
    marker_signature: str
    initialization_mode: str = "MARKER_PREVIEW"
    marker_geometry_dependencies: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """초기화 모드와 정렬 스냅샷·마커 형상 종속성의 관계를 검증합니다."""

        moving_model_id = str(self.moving_model_id).strip()
        reference_model_ids = tuple(str(model_id).strip() for model_id in self.reference_model_ids)
        if not moving_model_id or not reference_model_ids or any(
            not model_id for model_id in reference_model_ids
        ):
            raise ValueError("ICP seed 모델 식별자가 올바르지 않습니다.")
        if len(set(reference_model_ids)) != len(reference_model_ids):
            raise ValueError("ICP seed 기준 모델 ID는 중복될 수 없습니다.")
        initial = _freeze_float64_array(
            self.initial_alignment_float64,
            field_name="ICP 초기 정렬",
            shape=(4, 4),
        )
        base = _freeze_float64_array(
            self.base_committed_alignment_float64,
            field_name="ICP 확정 정렬 기준값",
            shape=(4, 4),
        )
        initialization_mode = str(self.initialization_mode).strip().upper()
        if initialization_mode not in {"MARKER_PREVIEW", "CURRENT_ALIGNMENT"}:
            raise ValueError("ICP 초기화 모드가 올바르지 않습니다.")
        if initialization_mode == "MARKER_PREVIEW":
            if not isinstance(self.marker_result, MarkerAlignmentResult):
                raise TypeError("마커 초기화 ICP seed에는 MarkerAlignmentResult가 필요합니다.")
            if not np.allclose(
                initial,
                self.marker_result.preview_alignment.matrix_float64,
                rtol=0.0,
                atol=1.0e-9,
            ):
                raise ValueError("ICP 초기 정렬은 marker preview alignment와 같아야 합니다.")
            if not np.allclose(
                base,
                self.marker_result.base_alignment.matrix_float64,
                rtol=0.0,
                atol=1.0e-9,
            ):
                raise ValueError("ICP 확정 정렬 기준값이 marker result와 다릅니다.")
        else:
            if self.marker_result is not None:
                raise TypeError("현재 정렬 초기화 ICP seed에는 marker result를 넣을 수 없습니다.")
            if not np.allclose(initial, base, rtol=0.0, atol=1.0e-9):
                raise ValueError("현재 정렬 초기화에서는 초기값과 확정 정렬 기준값이 같아야 합니다.")
        revisions = tuple((str(model_id).strip(), int(revision)) for model_id, revision in self.model_revision_snapshot)
        signature = str(self.marker_signature).strip()
        if not revisions or not signature:
            raise ValueError("ICP seed snapshot 정보가 비어 있습니다.")
        if self.marker_result is not None and signature != self.marker_result.marker_signature:
            raise ValueError("ICP seed marker signature가 marker result와 다릅니다.")
        object.__setattr__(self, "moving_model_id", moving_model_id)
        object.__setattr__(self, "reference_model_ids", reference_model_ids)
        object.__setattr__(self, "initial_alignment_float64", initial)
        object.__setattr__(self, "base_committed_alignment_float64", base)
        object.__setattr__(self, "model_revision_snapshot", revisions)
        object.__setattr__(self, "marker_signature", signature)
        object.__setattr__(self, "initialization_mode", initialization_mode)
        dependencies = _normalize_marker_geometry_dependencies(self.marker_geometry_dependencies)
        if initialization_mode == "CURRENT_ALIGNMENT" and dependencies:
            raise ValueError("직접 정렬 초기값에는 마커 형상 종속성을 넣을 수 없습니다.")
        allowed_models = {moving_model_id, *reference_model_ids}
        if any(model_id not in allowed_models for model_id, _source in dependencies):
            raise ValueError("마커 형상 종속성이 계산 대상 모델을 벗어났습니다.")
        object.__setattr__(self, "marker_geometry_dependencies", dependencies)

    @property
    def uses_marker_preview(self) -> bool:
        """ICP 초기값이 유효한 마커 미리보기에서 왔는지 반환합니다."""

        return self.initialization_mode == "MARKER_PREVIEW"

__all__ = (
    "AlignmentProvenanceRecord",
    "IcpAlignmentSeed",
    "IcpBackendUnavailableError",
    "MarkerAlignmentError",
    "MarkerAlignmentInput",
    "MarkerAlignmentResult",
    "MarkerDegeneracyError",
    "MarkerInputError",
    "MarkerNumericalError",
    "MarkerPreviewStaleError",
    "MarkerResidualMetrics",
    "RepresentativeTriangle",
    "Sim3Transform",
    "compose_sim3_transforms",
    "create_sim3_transform",
    "identity_sim3_transform",
    "transform_positions",
)
