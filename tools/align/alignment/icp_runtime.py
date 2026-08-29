"""메인 Blender 프로세스의 ICP 작업 상태·미리보기·승인 경계를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import hashlib
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np

from ..constants import (
    ICP_ALGORITHM_VERSION,
    ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    ICP_ALIGNMENT_PHASE_COMMITTED,
    ICP_ALIGNMENT_PHASE_FAILED,
    ICP_ALIGNMENT_PHASE_IDLE,
    ICP_ALIGNMENT_PHASE_PREPARING,
    ICP_ALIGNMENT_PHASE_PREVIEW,
    ICP_ALIGNMENT_PHASE_RUNNING,
    ICP_CANCEL_GRACE_SECONDS,
    ICP_STAGE_BUILDING_UNIFORM_SAMPLES,
    ICP_STAGE_CANCELLED,
    ICP_STAGE_COARSE_POINT_TO_POINT,
    ICP_STAGE_COMPLETED_PASS,
    ICP_STAGE_COMPLETED_REVIEW,
    ICP_STAGE_ESTIMATING_NORMALS_COARSE,
    ICP_STAGE_ESTIMATING_NORMALS_FINE,
    ICP_STAGE_ESTIMATING_NORMALS_MEDIUM,
    ICP_STAGE_EVALUATING_BEST_CANDIDATE,
    ICP_STAGE_FAILED,
    ICP_STAGE_FINE_POINT_TO_PLANE,
    ICP_STAGE_MEDIUM_POINT_TO_PLANE,
    ICP_STAGE_PREPARING_INPUT,
    ICP_STAGE_SCALE_REFINEMENT,
    ICP_STAGE_SELECTING_OVERLAP,
    MODEL_STATUS_REGISTERED,
)
from ..logging_utils import get_logger
from ..geometry_access import registration_point_ids_for_model, registration_positions_for_model
from ..model_repository import ModelRepositoryError
from ..undo import UndoJournal
from .icp_backend import (
    IcpBackendError,
    IcpJobFiles,
    IcpJobHandle,
    IcpJobProtocolError,
    build_manifest,
    cleanup_job_directory,
    cleanup_unstarted_job_directory,
    close_array_memmap,
    create_icp_job_directory,
    kill_job_worker,
    launch_icp_worker,
    launch_open3d_worker,
    read_array_file,
    read_job_error,
    read_job_progress,
    read_job_result,
    request_job_cancel,
    terminate_job_worker,
    validate_hybrid_result_metadata,
    wait_for_job_worker,
    write_array_atomically,
    write_job_manifest,
    write_point_ids_in_chunks_atomically,
    write_transformed_positions_atomically,
)
from .icp_external_contract import Open3dIcpError, analyze_external_icp_delta
from .icp_confidence import (
    DEFAULT_CONFIDENCE_NEIGHBORS,
    PointDistributionConfidence,
    compute_point_distribution_confidence,
)
from .icp_types import IcpError
from .icp_solver_backend import (
    IcpSolverBackend,
    backend_algorithm_version,
    is_external_python_backend,
    normalize_solver_backend,
)
from .runtime import (
    MarkerAlignmentPreviewState,
    _repository_for_runtime,
    create_icp_seed,
    icp_seed_is_current,
)
from .types import (
    AlignmentProvenanceRecord,
    IcpAlignmentSeed,
    MarkerAlignmentError,
    MarkerInputError,
    MarkerPreviewStaleError,
    Sim3Transform,
    transform_positions,
)


_LOGGER = get_logger("alignment.icp_runtime")
_ICP_COMMAND_COMMIT = "ICP_ALIGNMENT_COMMIT"
_ARRAY_MOVING_POSITIONS = "moving_positions.npy"
_ARRAY_MOVING_POINT_IDS = "moving_point_ids.npy"
_ARRAY_MOVING_CONFIDENCE_WEIGHTS = "moving_confidence_weights.npy"
_ARRAY_MOVING_ISOLATION_CANDIDATES = "moving_isolation_candidates.npy"
_MAX_EXTERNAL_JSON_INTEGER = int(np.iinfo(np.int64).max)
_HYBRID_PROGRESS_STAGES = frozenset(
    {
        ICP_STAGE_PREPARING_INPUT,
        ICP_STAGE_SELECTING_OVERLAP,
        ICP_STAGE_BUILDING_UNIFORM_SAMPLES,
        ICP_STAGE_ESTIMATING_NORMALS_COARSE,
        ICP_STAGE_COARSE_POINT_TO_POINT,
        ICP_STAGE_ESTIMATING_NORMALS_MEDIUM,
        ICP_STAGE_MEDIUM_POINT_TO_PLANE,
        ICP_STAGE_ESTIMATING_NORMALS_FINE,
        ICP_STAGE_FINE_POINT_TO_PLANE,
        ICP_STAGE_SCALE_REFINEMENT,
        ICP_STAGE_EVALUATING_BEST_CANDIDATE,
        ICP_STAGE_COMPLETED_PASS,
        ICP_STAGE_COMPLETED_REVIEW,
        ICP_STAGE_FAILED,
        ICP_STAGE_CANCELLED,
    }
)
_OPEN3D_PROGRESS_STAGES = frozenset(
    {
        "OPEN3D_PREPARE",
        "OPEN3D_BUILD_FULL_CLOUD",
        "OPEN3D_GICP_COARSE_DISTANCE",
        "OPEN3D_GICP_MEDIUM_DISTANCE",
        "OPEN3D_GICP_FINE_DISTANCE",
        "OPEN3D_ROBUST_POINT_TO_PLANE_COARSE_DISTANCE",
        "OPEN3D_ROBUST_POINT_TO_PLANE_MEDIUM_DISTANCE",
        "OPEN3D_ROBUST_POINT_TO_PLANE_FINE_DISTANCE",
        "OPEN3D_EVALUATE",
        "OPEN3D_COMPLETED_PASS",
        "OPEN3D_COMPLETED_REVIEW",
        ICP_STAGE_FAILED,
        ICP_STAGE_CANCELLED,
    }
)
_SMALL_GICP_PROGRESS_STAGES = frozenset(
    {
        "SMALL_GICP_PREPARE",
        "SMALL_GICP_BUILD_FULL_CLOUD",
        "SMALL_GICP_GICP_COARSE_DISTANCE",
        "SMALL_GICP_GICP_MEDIUM_DISTANCE",
        "SMALL_GICP_GICP_FINE_DISTANCE",
        "SMALL_GICP_EVALUATE",
        "SMALL_GICP_SCALE_REFINEMENT",
        "SMALL_GICP_SECOND_GICP_FINE_DISTANCE",
        "SMALL_GICP_COMPLETED_PASS",
        "SMALL_GICP_COMPLETED_REVIEW",
        ICP_STAGE_FAILED,
        ICP_STAGE_CANCELLED,
    }
)
_EXTERNAL_PYTHON_BACKENDS = frozenset(
    {"OPEN3D_GICP", "SMALL_GICP", "OPEN3D_ROBUST_POINT_TO_PLANE"}
)
_OPEN3D_BACKENDS = frozenset(
    {"OPEN3D_GICP", "OPEN3D_ROBUST_POINT_TO_PLANE"}
)

if TYPE_CHECKING:
    from ..state import MarkerSelectionSnapshot, ModelCatalogSnapshot


class IcpRuntimeError(RuntimeError):
    """ICP 메인 프로세스 상태·입력·승인 오류를 구분합니다."""


class IcpRuntimeStaleError(IcpRuntimeError):
    """worker 결과 또는 승인 입력이 시작 snapshot과 달라졌음을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class IcpProgressState:
    """메인 UI가 읽는 worker 진행률의 검증된 작은 상태를 보관합니다."""

    stage: str
    level_name: str
    iteration: int
    progress: float
    message: str
    stable_alignment: Sim3Transform | None = None
    max_iterations: int = 0
    level_complete: bool = False
    objective_kind: str | None = None
    maximum_correspondence_distance: float | None = None
    correspondence_count: int | None = None
    point_to_point_rmse: float | None = None
    point_to_plane_rmse: float | None = None
    normal_valid_count: int | None = None
    normal_angle_pass_count: int | None = None
    normal_radius: float | None = None
    reference_sample_count: int | None = None
    moving_sample_count: int | None = None
    backend_id: str = "CUSTOM_HYBRID"
    backend_algorithm_version: str = ICP_ALGORITHM_VERSION
    open3d_version: str = ""
    small_gicp_version: str = ""


@dataclass(frozen=True, slots=True)
class IcpRuntimeInput:
    """worker 파일·stale 검증·평가 전처리 감사를 보관합니다."""

    seed: IcpAlignmentSeed
    snapshot: Any | None
    parameters: Any
    input_fingerprint: str
    model_state_snapshot: tuple[tuple[str, int, str, str], ...]
    reference_model_ids: tuple[str, ...]
    model_revision_snapshot: tuple[tuple[str, int], ...]
    algorithm_version: str
    moving_cloud_frame: str
    solver_backend: str = "CUSTOM_HYBRID"
    backend_algorithm_version: str = ICP_ALGORITHM_VERSION
    confidence_preprocessing: Mapping[str, Any] = field(default_factory=dict)

    @property
    def moving_positions(self) -> np.ndarray:
        """중복 복사 없이 core snapshot의 P 좌표 배열을 반환합니다."""

        if self.snapshot is None:
            raise IcpRuntimeError("실행 중 ICP 입력은 main 메모리에 전체 P 배열을 보관하지 않습니다.")
        return self.snapshot.moving_cloud.positions_float64

    @property
    def moving_point_ids(self) -> np.ndarray:
        """중복 복사 없이 core snapshot의 P ID 배열을 반환합니다."""

        if self.snapshot is None:
            raise IcpRuntimeError("실행 중 ICP 입력은 main 메모리에 전체 P ID 배열을 보관하지 않습니다.")
        return self.snapshot.moving_cloud.point_ids_uint64

    @property
    def reference_positions(self) -> tuple[np.ndarray, ...]:
        """중복 복사 없이 core snapshot의 A 좌표 배열을 반환합니다."""

        if self.snapshot is None:
            raise IcpRuntimeError("실행 중 ICP 입력은 main 메모리에 전체 A 배열을 보관하지 않습니다.")
        return tuple(cloud.positions_float64 for cloud in self.snapshot.reference_clouds)

    @property
    def reference_point_ids(self) -> tuple[np.ndarray, ...]:
        """중복 복사 없이 core snapshot의 A ID 배열을 반환합니다."""

        if self.snapshot is None:
            raise IcpRuntimeError("실행 중 ICP 입력은 main 메모리에 전체 A ID 배열을 보관하지 않습니다.")
        return tuple(cloud.point_ids_uint64 for cloud in self.snapshot.reference_clouds)


@dataclass(frozen=True, slots=True)
class IcpRuntimeResult:
    """worker가 반환한 결과의 미리보기·UI·승인용 안전한 표현입니다."""

    moving_model_id: str
    reference_model_ids: tuple[str, ...]
    final_alignment: Sim3Transform
    icp_delta: Sim3Transform
    quality: Mapping[str, Any]
    metadata: Mapping[str, Any]
    input_fingerprint: str
    parameter_fingerprint: str
    marker_signature: str
    model_revision_snapshot: tuple[tuple[str, int], ...]
    algorithm_version: str
    raw_result: Any = None
    solver_backend: str = "CUSTOM_HYBRID"
    backend_algorithm_version: str = ICP_ALGORITHM_VERSION

    def _result_value(self, field_name: str) -> Any:
        """검증된 core 결과를 우선해 UI용 진단값을 반환합니다."""

        if self.raw_result is not None and hasattr(self.raw_result, field_name):
            value = getattr(self.raw_result, field_name)
            if value is not None:
                return value
        return self.metadata.get(field_name)

    def _result_mapping(self, field_name: str) -> Mapping[str, Any] | None:
        """중첩 core 진단 객체를 JSON 안전 mapping으로 보존해 반환합니다."""

        value = self._result_value(field_name)
        if value is None:
            return None
        if hasattr(value, "to_mapping"):
            value = value.to_mapping()
        return value if isinstance(value, Mapping) else None

    @property
    def quality_status(self) -> str:
        """PASS·REVIEW 승인 판정에 쓸 정규화된 품질 상태를 반환합니다."""

        return str(self.quality.get("status", "")).strip().upper()

    @property
    def is_approvable(self) -> bool:
        """유효한 PASS 또는 REVIEW ICP 후보인지 반환합니다."""

        return self.quality_status in {"PASS", "REVIEW"}

    @property
    def best_candidate(self) -> Mapping[str, Any] | None:
        """실제 미리보기에 적용한 최선 ICP 후보 진단값을 반환합니다."""

        return self._result_mapping("best_candidate")

    @property
    def baseline_metrics(self) -> Mapping[str, Any] | None:
        """마커 초기값의 고정 평가 지표를 반환합니다."""

        return self._result_mapping("baseline_metrics")

    @property
    def best_candidate_metrics(self) -> Mapping[str, Any] | None:
        """최선 ICP 후보의 고정 평가 지표를 반환합니다."""

        return self._result_mapping("best_candidate_metrics")

    @property
    def density_sampling_metrics(self) -> Mapping[str, Any] | None:
        """균일 샘플의 voxel·macro-cell 분포 지표를 반환합니다."""

        return self._result_mapping("density_sampling_metrics")

    @property
    def relative_improvement(self) -> float:
        """baseline 대비 최선 후보의 상대 개선율을 반환합니다."""

        return float(self._result_value("relative_improvement"))

    @property
    def best_level_name(self) -> str:
        """최선 후보를 고른 해상도 레벨 이름을 반환합니다."""

        return str(self._result_value("best_level_name"))

    @property
    def best_iteration(self) -> int:
        """최선 후보의 반복 번호를 반환합니다."""

        return int(self._result_value("best_iteration"))

    @property
    def solver_stop_reason(self) -> str:
        """최종 solver 또는 최선 레벨 종료 사유를 반환합니다."""

        value = self._result_value("solver_stop_reason")
        return str(getattr(value, "value", value)).strip()

    @property
    def point_to_plane_rmse(self) -> float:
        """최선 후보의 point-to-plane RMSE를 반환합니다."""

        return float(self._result_value("point_to_plane_rmse"))

    @property
    def normal_valid_ratio(self) -> float:
        """최종 평가에서 유효 노멀 비율을 반환합니다."""

        return float(self._result_value("normal_valid_ratio"))

    @property
    def normal_angle_pass_ratio(self) -> float:
        """최종 평가에서 노멀 각도 gate 통과 비율을 반환합니다."""

        return float(self._result_value("normal_angle_pass_ratio"))

    @property
    def user_review_required(self) -> bool:
        """사용자 검토 안내를 표시해야 하는 REVIEW 후보인지 반환합니다."""

        return bool(self._result_value("user_review_required"))


def is_icp_result_approvable(result: IcpRuntimeResult | None) -> bool:
    """UI·승인 경계가 PASS와 REVIEW 후보를 함께 허용하는지 반환합니다."""

    return result is not None and result.is_approvable


@dataclass(frozen=True, slots=True)
class IcpMarkerPreviewSnapshot:
    """ICP 승인 Undo가 복원할 marker preview 전용 상태를 보관합니다."""

    phase: str
    result: Any
    last_error: str
    started_at: float | None
    p_view_snapshot: Any


@dataclass(frozen=True, slots=True)
class IcpCommitStateSnapshot:
    """한 방향의 ICP 승인 Undo 상태를 원자 복원 가능한 형태로 보관합니다."""

    moving_model_id: str
    alignment: Sim3Transform
    catalog_snapshot: "ModelCatalogSnapshot"
    marker_selection: "MarkerSelectionSnapshot"
    marker_preview: IcpMarkerPreviewSnapshot


@dataclass(frozen=True, slots=True)
class IcpAlignmentCommitSnapshot:
    """ICP 최종 승인 한 건의 전후 상태와 결과 식별자를 보관합니다."""

    before: IcpCommitStateSnapshot
    after: IcpCommitStateSnapshot
    input_fingerprint: str
    result_fingerprint: str


@dataclass
class IcpAlignmentRuntimeState:
    """marker preview와 독립적으로 ICP 실행·결과 미리보기를 관리합니다."""

    phase: str = ICP_ALIGNMENT_PHASE_IDLE
    job_handle: IcpJobHandle | None = None
    input_data: IcpRuntimeInput | None = None
    progress: IcpProgressState | None = None
    result: IcpRuntimeResult | None = None
    working_alignment: Sim3Transform | None = None
    last_error: str = ""
    started_at: float | None = None
    cancel_requested_at: float | None = None
    termination_requested_at: float | None = None
    p_view_snapshot: Any = None
    diagnostic_handles: list[IcpJobHandle] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        """입력 잠금이 필요한 실제 worker 실행 단계인지 반환합니다."""

        return self.phase in {
            ICP_ALIGNMENT_PHASE_PREPARING,
            ICP_ALIGNMENT_PHASE_RUNNING,
            ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
        }

    @property
    def has_preview(self) -> bool:
        """P에 ICP final transform을 비파괴로 표시할 수 있는지 반환합니다."""

        return self.phase == ICP_ALIGNMENT_PHASE_PREVIEW and is_icp_result_approvable(self.result)

    def clear(self, *, error_message: str = "") -> None:
        """job 참조를 제외한 ICP 미리보기 상태를 초기값으로 되돌립니다."""

        self.phase = ICP_ALIGNMENT_PHASE_IDLE
        self.job_handle = None
        self.input_data = None
        self.progress = None
        self.result = None
        self.working_alignment = None
        self.last_error = str(error_message).strip()
        self.started_at = None
        self.cancel_requested_at = None
        self.termination_requested_at = None
        self.p_view_snapshot = None


def _icp_state(runtime: Any) -> IcpAlignmentRuntimeState:
    """Runtime이 소유한 typed ICP 상태를 검증해 반환합니다."""

    state = getattr(runtime, "icp_alignment", None)
    if not isinstance(state, IcpAlignmentRuntimeState):
        raise IcpRuntimeError("ICP 런타임 상태가 초기화되지 않았습니다.")
    return state


def _parameter_enum_value(enum_type: Any, value: str) -> Any:
    """문자열 UI 값을 core enum의 이름 또는 값으로 엄격하게 변환합니다."""

    normalized = str(value).strip().upper()
    try:
        return enum_type[normalized]
    except KeyError:
        pass
    for item in enum_type:
        if str(getattr(item, "value", "")).upper() == normalized:
            return item
    raise IcpRuntimeError(f"지원하지 않는 ICP 설정 값입니다: {value}")


def _create_parameters(preset: str, scale_refine_enabled: bool) -> Any:
    """core가 제공한 명시 타입으로 UI 프리셋과 scale 모드를 고정합니다."""

    try:
        from .icp_types import IcpParameters, IcpPreset, IcpScaleMode
    except ImportError as error:
        raise IcpRuntimeError("ICP 수치 설정 모듈을 불러오지 못했습니다.") from error
    # 기존 저장 Scene의 QUICK 값을 공개 FAST 프리셋으로 한 번만 호환 변환합니다.
    normalized_preset = "FAST" if str(preset).strip().upper() == "QUICK" else preset
    preset_value = _parameter_enum_value(IcpPreset, normalized_preset)
    try:
        return IcpParameters.for_preset(
            preset_value,
            scale_refine_enabled=bool(scale_refine_enabled),
        )
    except (TypeError, ValueError) as error:
        raise IcpRuntimeError("ICP 프리셋 설정을 만들지 못했습니다.") from error


def _json_value(value: Any) -> Any:
    """불변 core 타입을 worker manifest에 넣을 JSON 기본 타입으로 변환합니다."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IcpRuntimeError("ICP worker manifest에 기록할 수 없는 설정 값이 있습니다.")


def _validated_positions_view(value: object, *, field_name: str) -> np.ndarray:
    """원본을 바꾸지 않고 변환에 사용할 유효한 float64 좌표 뷰를 준비합니다."""

    positions = np.asarray(value, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 3 or not np.isfinite(positions).all():
        raise IcpRuntimeError(f"{field_name} 점 좌표가 유한한 N×3 배열이 아닙니다.")
    return np.ascontiguousarray(positions, dtype=np.float64)


def _validated_point_ids_view(
    value: object,
    *,
    expected_count: int,
    field_name: str,
) -> np.ndarray:
    """원본을 바꾸지 않고 core 입력에 사용할 유효한 uint64 ID 뷰를 준비합니다."""

    source = np.asarray(value)
    if source.dtype.kind not in {"u", "i"}:
        raise IcpRuntimeError(f"{field_name} 등록점 ID dtype가 정수가 아닙니다.")
    if source.dtype.kind == "i" and np.any(source < 0):
        raise IcpRuntimeError(f"{field_name} 등록점 ID에 음수 값이 있습니다.")
    point_ids = np.asarray(source, dtype=np.uint64)
    if point_ids.shape != (expected_count,):
        raise IcpRuntimeError(f"{field_name} 등록점 ID 길이가 점 좌표와 다릅니다.")
    return np.ascontiguousarray(point_ids, dtype=np.uint64)


def _apply_matrix_to_positions(matrix: object, positions: object, *, field_name: str) -> np.ndarray:
    """원본을 복사하지 않고 column-vector 4×4 행렬을 점 좌표에 적용합니다."""

    transform = np.array(matrix, dtype=np.float64, copy=True, order="C")
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise IcpRuntimeError(f"{field_name} 변환 행렬이 올바르지 않습니다.")
    source = _validated_positions_view(positions, field_name=field_name)
    transformed = np.empty((source.shape[0], 3), dtype=np.float64, order="C")
    np.matmul(source, transform[:3, :3].T, out=transformed)
    np.add(transformed, transform[:3, 3], out=transformed)
    transformed.setflags(write=False)
    return transformed


def _model_state_snapshot(runtime: Any, model_ids: tuple[str, ...]) -> tuple[tuple[str, int, str, str], ...]:
    """geometry·표시축·확정 alignment를 stale 검사용 안정 snapshot으로 고정합니다."""

    repository = _repository_for_runtime(runtime)
    snapshots: list[tuple[str, int, str, str]] = []
    for model_id in sorted(set(model_ids)):
        model = repository.models.get(model_id)
        if model is None:
            raise IcpRuntimeError(f"ICP 입력 모델을 찾을 수 없습니다: {model_id}")
        revision = int(getattr(model, "geometry_revision", -1))
        if revision < 0:
            raise IcpRuntimeError(f"ICP 입력 모델 geometry revision이 올바르지 않습니다: {model_id}")
        display = repository.display_transform_for_model_id(model_id)
        alignment = repository.alignment_transform_for_model_id(model_id)
        snapshots.append((model_id, revision, display.fingerprint, alignment.fingerprint))
    return tuple(snapshots)


def _active_reference_ids(runtime: Any, moving_model_id: str) -> tuple[str, ...]:
    """숨김 여부와 무관하게 등록 A 그룹 전체를 카탈로그 순서로 반환합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    active_models = getattr(catalog, "active_models", None)
    if not callable(active_models):
        raise IcpRuntimeError("ICP 기준 A 데이터 레이어를 읽을 수 없습니다.")
    model_ids = tuple(
        str(record.model_id)
        for record in active_models()
        if str(getattr(record, "registration_status", "")) == MODEL_STATUS_REGISTERED
        and str(getattr(record, "model_id", "")) != moving_model_id
    )
    if not model_ids:
        raise IcpRuntimeError("ICP 기준 A 데이터 레이어가 없습니다.")
    return model_ids


def _reference_ids_for_seed(runtime: Any, seed: IcpAlignmentSeed) -> tuple[str, ...]:
    """직접·마커 Align 모두 같은 A 그룹 전체 기준 목록을 반환합니다."""

    active_ids = _active_reference_ids(runtime, seed.moving_model_id)
    if tuple(seed.reference_model_ids) != active_ids:
        raise IcpRuntimeError("ICP seed의 A 그룹 기준 목록이 현재 카탈로그와 다릅니다.")
    return active_ids


def _build_core_snapshot(
    *,
    seed: IcpAlignmentSeed,
    parameters: Any,
    moving_cloud: Any,
    reference_model_ids: tuple[str, ...],
    reference_clouds: tuple[Any, ...],
    model_revision_snapshot: tuple[tuple[str, int], ...],
    moving_cloud_frame: str = "SOURCE_LOCAL",
) -> Any:
    """이미 고정된 core point cloud로 bpy 없는 ICP snapshot을 조립합니다."""

    try:
        from .icp_types import IcpInputSnapshot
    except ImportError as error:
        raise IcpRuntimeError("ICP 수치 입력 타입을 불러오지 못했습니다.") from error
    if moving_cloud.model_id != seed.moving_model_id:
        raise IcpRuntimeError("ICP 이동 모델 ID가 초기 정렬 seed와 다릅니다.")
    if tuple(cloud.model_id for cloud in reference_clouds) != reference_model_ids:
        raise IcpRuntimeError("ICP 기준 모델 ID와 점군 순서가 다릅니다.")
    marker_result = seed.marker_result
    if marker_result is None:
        marker_max_error = 0.0
        marker_rms = 0.0
        marker_reference_project = None
        moving_marker_display = None
    else:
        base_alignment = Sim3Transform(matrix_float64=seed.base_committed_alignment_float64)
        base_inverse = Sim3Transform(matrix_float64=base_alignment.inverse_float64)
        marker_max_error = marker_result.metrics.maximum
        marker_rms = marker_result.metrics.rms
        marker_reference_project = marker_result.representative_triangle.reference_vertices
        moving_marker_display = transform_positions(
            base_inverse,
            marker_result.representative_triangle.moving_vertices,
        )
    return IcpInputSnapshot(
        moving_model_id=seed.moving_model_id,
        reference_model_ids=reference_model_ids,
        moving_cloud=moving_cloud,
        reference_clouds=reference_clouds,
        initial_alignment=Sim3Transform(matrix_float64=seed.initial_alignment_float64),
        parameters=parameters,
        marker_uniform_scale=Sim3Transform(
            matrix_float64=seed.initial_alignment_float64
        ).uniform_scale,
        marker_max_error=marker_max_error,
        marker_rms=marker_rms,
        marker_reference_project=marker_reference_project,
        marker_moving_project=moving_marker_display,
        marker_signature=seed.marker_signature,
        model_revision_snapshot=model_revision_snapshot,
        moving_cloud_frame=moving_cloud_frame,
    )


def _create_core_cloud(
    *,
    model_id: str,
    positions: object,
    point_ids: object,
    field_name: str,
) -> Any:
    """변환 직후 임시 배열을 하나의 canonical core point cloud로 고정합니다."""

    try:
        from .icp_types import IcpPointCloud
    except ImportError as error:
        raise IcpRuntimeError("ICP 점군 타입을 불러오지 못했습니다.") from error
    position_view = _validated_positions_view(positions, field_name=field_name)
    point_id_view = _validated_point_ids_view(
        point_ids,
        expected_count=position_view.shape[0],
        field_name=field_name,
    )
    return IcpPointCloud(
        model_id=model_id,
        positions_float64=position_view,
        point_ids_uint64=point_id_view,
    )


def create_icp_input_snapshot(
    runtime: Any,
    preset: str = "BALANCED",
    scale_refine_enabled: bool = False,
) -> IcpRuntimeInput:
    """현재 초기 정렬과 실제 모델 배열에서 worker용 불변 ICP 입력을 생성합니다."""

    state = _icp_state(runtime)
    if state.phase not in {ICP_ALIGNMENT_PHASE_IDLE, ICP_ALIGNMENT_PHASE_FAILED, ICP_ALIGNMENT_PHASE_PREPARING}:
        raise IcpRuntimeError("이미 실행 중인 ICP 작업이 있습니다.")
    seed = create_icp_seed(runtime)
    parameters = _create_parameters(preset, scale_refine_enabled)
    repository = _repository_for_runtime(runtime)
    moving_model = repository.models.get(seed.moving_model_id)
    if moving_model is None:
        raise IcpRuntimeError("ICP 이동 P 모델을 찾을 수 없습니다.")
    moving_display = repository.display_transform_for_model_id(seed.moving_model_id)
    moving_source_positions = registration_positions_for_model(moving_model)
    moving_source_ids = registration_point_ids_for_model(moving_model)
    moving_positions = _apply_matrix_to_positions(
        moving_display.matrix_float64,
        moving_source_positions,
        field_name="ICP 이동 P",
    )
    moving_cloud = _create_core_cloud(
        model_id=seed.moving_model_id,
        positions=moving_positions,
        point_ids=moving_source_ids,
        field_name="ICP 이동 P",
    )
    reference_ids = _reference_ids_for_seed(runtime, seed)
    reference_clouds: list[Any] = []
    for reference_model_id in reference_ids:
        reference_model = repository.models.get(reference_model_id)
        if reference_model is None:
            raise IcpRuntimeError(f"ICP 기준 A 모델을 찾을 수 없습니다: {reference_model_id}")
        display = repository.display_transform_for_model_id(reference_model_id)
        alignment = repository.alignment_transform_for_model_id(reference_model_id)
        world_matrix = alignment.matrix_float64 @ display.matrix_float64
        reference_source_positions = registration_positions_for_model(reference_model)
        reference_source_ids = registration_point_ids_for_model(reference_model)
        positions = _apply_matrix_to_positions(
            world_matrix,
            reference_source_positions,
            field_name=f"ICP 기준 A {reference_model_id}",
        )
        reference_clouds.append(
            _create_core_cloud(
                model_id=reference_model_id,
                positions=positions,
                point_ids=reference_source_ids,
                field_name=f"ICP 기준 A {reference_model_id}",
            )
        )
    model_snapshot = _model_state_snapshot(runtime, (seed.moving_model_id, *reference_ids))
    core_revisions = tuple((model_id, revision) for model_id, revision, _display, _alignment in model_snapshot)
    snapshot = _build_core_snapshot(
        seed=seed,
        parameters=parameters,
        moving_cloud=moving_cloud,
        reference_model_ids=reference_ids,
        reference_clouds=tuple(reference_clouds),
        model_revision_snapshot=core_revisions,
        moving_cloud_frame="SOURCE_LOCAL",
    )
    fingerprint = snapshot.input_fingerprint
    return IcpRuntimeInput(
        seed=seed,
        snapshot=snapshot,
        parameters=parameters,
        input_fingerprint=fingerprint,
        model_state_snapshot=model_snapshot,
        reference_model_ids=reference_ids,
        model_revision_snapshot=core_revisions,
        algorithm_version=snapshot.algorithm_version,
        moving_cloud_frame=str(snapshot.moving_cloud_frame.value),
    )


def _array_manifest_entries(input_data: IcpRuntimeInput, files: IcpJobFiles) -> tuple[dict[str, Any], tuple[str, ...]]:
    """입력 배열을 작업 폴더에 기록하고 manifest 파일 항목을 반환합니다."""

    write_array_atomically(
        files.job_directory / _ARRAY_MOVING_POSITIONS,
        input_data.moving_positions,
        dtype=np.dtype(np.float64),
        shape=(None, 3),
    )
    write_array_atomically(
        files.job_directory / _ARRAY_MOVING_POINT_IDS,
        input_data.moving_point_ids,
        dtype=np.dtype(np.uint64),
        shape=(input_data.moving_positions.shape[0],),
    )
    reference_entries: list[dict[str, str]] = []
    reference_ids: list[str] = []
    for index, (model_id, positions, point_ids) in enumerate(
        zip(
            input_data.reference_model_ids,
            input_data.reference_positions,
            input_data.reference_point_ids,
            strict=True,
        )
    ):
        positions_name = f"reference_{index}_positions.npy"
        point_ids_name = f"reference_{index}_point_ids.npy"
        write_array_atomically(
            files.job_directory / positions_name,
            positions,
            dtype=np.dtype(np.float64),
            shape=(None, 3),
        )
        write_array_atomically(
            files.job_directory / point_ids_name,
            point_ids,
            dtype=np.dtype(np.uint64),
            shape=(positions.shape[0],),
        )
        reference_entries.append(
            {
                "model_id": str(model_id),
                "positions_file": positions_name,
                "point_ids_file": point_ids_name,
            }
        )
        reference_ids.append(str(model_id))
    return (
        {
            "moving": {
                "model_id": input_data.seed.moving_model_id,
                "positions_file": _ARRAY_MOVING_POSITIONS,
                "point_ids_file": _ARRAY_MOVING_POINT_IDS,
            },
            "references": reference_entries,
        },
        tuple(reference_ids),
    )


def _point_distribution_confidence_for_model(
    repository: Any,
    model_id: str,
) -> tuple[PointDistributionConfidence, bool, float]:
    """원본 revision 캐시를 조회하고 없을 때만 점 분포 신뢰도를 계산합니다."""

    model = repository.models.get(model_id)
    if model is None:
        raise IcpRuntimeError(f"ICP 점 분포 신뢰도를 계산할 모델이 없습니다: {model_id}")
    positions = registration_positions_for_model(model)
    cached = repository.cached_icp_confidence_for_model_id(model_id)
    if isinstance(cached, PointDistributionConfidence):
        if cached.point_count != int(positions.shape[0]):
            raise IcpRuntimeError("캐시된 ICP 점 분포 신뢰도 길이가 현재 원본점 수와 다릅니다.")
        return cached, True, 0.0
    started_at = monotonic()
    try:
        confidence = compute_point_distribution_confidence(
            positions,
            k=DEFAULT_CONFIDENCE_NEIGHBORS,
        )
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpRuntimeError(
            f"ICP 점 분포 신뢰도를 계산하지 못했습니다: {model_id}"
        ) from error
    elapsed_seconds = monotonic() - started_at
    repository.cache_icp_confidence_for_model_id(
        model_id,
        geometry_revision=int(getattr(model, "geometry_revision", -1)),
        confidence=confidence,
    )
    return confidence, False, elapsed_seconds


def _write_confidence_arrays(
    files: IcpJobFiles,
    confidence: PointDistributionConfidence,
    *,
    weights_name: str,
    candidates_name: str,
) -> None:
    """캐시된 신뢰도와 고립 후보 mask를 worker용 NPY로 원자 기록합니다."""

    write_array_atomically(
        files.job_directory / weights_name,
        confidence.weights_float64,
        dtype=np.dtype(np.float64),
        shape=(confidence.point_count,),
    )
    write_array_atomically(
        files.job_directory / candidates_name,
        confidence.isolation_candidates_bool.astype(np.uint8, copy=False),
        dtype=np.dtype(np.uint8),
        shape=(confidence.point_count,),
    )


def _write_project_world_job_arrays(
    runtime: Any,
    files: IcpJobFiles,
    *,
    moving_model_id: str,
    reference_model_ids: tuple[str, ...],
    moving_initial_alignment: Sim3Transform,
    include_confidence: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project World 등록점과 선택적인 원본 분포 신뢰도 NPY를 기록합니다."""

    repository = _repository_for_runtime(runtime)
    confidence_audit: list[dict[str, Any]] = []
    moving_model = repository.models.get(moving_model_id)
    if moving_model is None:
        raise IcpRuntimeError("ICP 이동 P 모델을 찾을 수 없습니다.")
    moving_display = repository.display_transform_for_model_id(moving_model_id)
    moving_positions = registration_positions_for_model(moving_model)
    moving_point_ids = registration_point_ids_for_model(moving_model)
    write_transformed_positions_atomically(
        files.job_directory / _ARRAY_MOVING_POSITIONS,
        moving_positions,
        moving_initial_alignment.matrix_float64 @ moving_display.matrix_float64,
    )
    write_point_ids_in_chunks_atomically(
        files.job_directory / _ARRAY_MOVING_POINT_IDS,
        moving_point_ids,
    )
    moving_entry: dict[str, Any] = {
        "model_id": moving_model_id,
        "positions_file": _ARRAY_MOVING_POSITIONS,
        "point_ids_file": _ARRAY_MOVING_POINT_IDS,
    }
    if include_confidence:
        moving_confidence, cache_hit, elapsed_seconds = (
            _point_distribution_confidence_for_model(repository, moving_model_id)
        )
        _write_confidence_arrays(
            files,
            moving_confidence,
            weights_name=_ARRAY_MOVING_CONFIDENCE_WEIGHTS,
            candidates_name=_ARRAY_MOVING_ISOLATION_CANDIDATES,
        )
        moving_entry.update(
            {
                "confidence_weights_file": _ARRAY_MOVING_CONFIDENCE_WEIGHTS,
                "isolation_candidates_file": _ARRAY_MOVING_ISOLATION_CANDIDATES,
            }
        )
        confidence_audit.append(
            {
                "model_id": moving_model_id,
                "geometry_revision": int(getattr(moving_model, "geometry_revision", -1)),
                "point_count": moving_confidence.point_count,
                "neighbor_count": moving_confidence.neighbor_count,
                "isolation_candidate_count": moving_confidence.isolation_candidate_count,
                "cache_hit": cache_hit,
                "calculation_seconds": float(elapsed_seconds),
            }
        )
    reference_entries: list[dict[str, str]] = []
    for index, reference_model_id in enumerate(reference_model_ids):
        reference_model = repository.models.get(reference_model_id)
        if reference_model is None:
            raise IcpRuntimeError(f"ICP 기준 A 모델을 찾을 수 없습니다: {reference_model_id}")
        display = repository.display_transform_for_model_id(reference_model_id)
        alignment = repository.alignment_transform_for_model_id(reference_model_id)
        reference_positions = registration_positions_for_model(reference_model)
        reference_point_ids = registration_point_ids_for_model(reference_model)
        positions_name = f"reference_{index}_positions.npy"
        point_ids_name = f"reference_{index}_point_ids.npy"
        write_transformed_positions_atomically(
            files.job_directory / positions_name,
            reference_positions,
            alignment.matrix_float64 @ display.matrix_float64,
        )
        write_point_ids_in_chunks_atomically(
            files.job_directory / point_ids_name,
            reference_point_ids,
        )
        reference_entry: dict[str, str] = {
            "model_id": reference_model_id,
            "positions_file": positions_name,
            "point_ids_file": point_ids_name,
        }
        if include_confidence:
            confidence, cache_hit, elapsed_seconds = (
                _point_distribution_confidence_for_model(repository, reference_model_id)
            )
            weights_name = f"reference_{index}_confidence_weights.npy"
            candidates_name = f"reference_{index}_isolation_candidates.npy"
            _write_confidence_arrays(
                files,
                confidence,
                weights_name=weights_name,
                candidates_name=candidates_name,
            )
            reference_entry.update(
                {
                    "confidence_weights_file": weights_name,
                    "isolation_candidates_file": candidates_name,
                }
            )
            confidence_audit.append(
                {
                    "model_id": reference_model_id,
                    "geometry_revision": int(
                        getattr(reference_model, "geometry_revision", -1)
                    ),
                    "point_count": confidence.point_count,
                    "neighbor_count": confidence.neighbor_count,
                    "isolation_candidate_count": confidence.isolation_candidate_count,
                    "cache_hit": cache_hit,
                    "calculation_seconds": float(elapsed_seconds),
                }
            )
        reference_entries.append(reference_entry)
    return (
        {
            "moving": moving_entry,
            "references": reference_entries,
        },
        {
            "enabled": bool(include_confidence),
            "k": DEFAULT_CONFIDENCE_NEIGHBORS,
            "cache_hit_count": sum(bool(item["cache_hit"]) for item in confidence_audit),
            "cache_miss_count": sum(not bool(item["cache_hit"]) for item in confidence_audit),
            "calculation_seconds": float(
                sum(float(item["calculation_seconds"]) for item in confidence_audit)
            ),
            "models": confidence_audit,
        },
    )


def _job_snapshot_fingerprint(
    files: IcpJobFiles,
    *,
    seed: IcpAlignmentSeed,
    parameters: Any,
    reference_model_ids: tuple[str, ...],
    model_revision_snapshot: tuple[tuple[str, int], ...],
    array_entries: Mapping[str, Any],
    moving_cloud_frame: str,
) -> tuple[str, str]:
    """job NPY memmap으로 core fingerprint만 계산한 뒤 모든 main memmap을 닫습니다."""

    try:
        from .icp_types import ICP_ALGORITHM_VERSION, IcpPointCloud
    except ImportError as error:
        raise IcpRuntimeError("ICP job 입력 타입을 불러오지 못했습니다.") from error
    moving_entry = array_entries.get("moving")
    reference_entries = array_entries.get("references")
    if not isinstance(moving_entry, Mapping) or not isinstance(reference_entries, list):
        raise IcpJobProtocolError("ICP job 배열 항목이 올바르지 않습니다.")
    opened_arrays: list[np.ndarray] = []
    loaded_reference_clouds: list[Any] = []
    snapshot: Any = None
    moving_cloud: Any = None
    reference_clouds: tuple[Any, ...] = ()
    try:
        moving_positions = read_array_file(
            files.job_directory,
            str(moving_entry["positions_file"]),
            dtype=np.dtype(np.float64),
            shape=(None, 3),
            mmap_mode="r",
        )
        moving_ids = read_array_file(
            files.job_directory,
            str(moving_entry["point_ids_file"]),
            dtype=np.dtype(np.uint64),
            shape=(moving_positions.shape[0],),
            mmap_mode="r",
        )
        opened_arrays.extend((moving_positions, moving_ids))
        moving_cloud = IcpPointCloud.from_verified_arrays(
            model_id=seed.moving_model_id,
            positions_float64=moving_positions,
            point_ids_uint64=moving_ids,
        )
        for model_id, entry in zip(reference_model_ids, reference_entries, strict=True):
            if not isinstance(entry, Mapping) or str(entry.get("model_id", "")) != model_id:
                raise IcpJobProtocolError("ICP 기준 job 배열 순서가 모델 ID와 다릅니다.")
            positions = read_array_file(
                files.job_directory,
                str(entry["positions_file"]),
                dtype=np.dtype(np.float64),
                shape=(None, 3),
                mmap_mode="r",
            )
            point_ids = read_array_file(
                files.job_directory,
                str(entry["point_ids_file"]),
                dtype=np.dtype(np.uint64),
                shape=(positions.shape[0],),
                mmap_mode="r",
            )
            opened_arrays.extend((positions, point_ids))
            loaded_reference_clouds.append(
                IcpPointCloud.from_verified_arrays(
                    model_id=model_id,
                    positions_float64=positions,
                    point_ids_uint64=point_ids,
                )
            )
        reference_clouds = tuple(loaded_reference_clouds)
        snapshot = _build_core_snapshot(
            seed=seed,
            parameters=parameters,
            moving_cloud=moving_cloud,
            reference_model_ids=reference_model_ids,
            reference_clouds=reference_clouds,
            model_revision_snapshot=model_revision_snapshot,
            moving_cloud_frame=moving_cloud_frame,
        )
        return snapshot.input_fingerprint, str(ICP_ALGORITHM_VERSION)
    finally:
        # Windows에서는 memmap을 참조한 cloud가 남아 있으면 job 폴더를 삭제할 수 없습니다.
        snapshot = None
        moving_cloud = None
        reference_clouds = ()
        loaded_reference_clouds.clear()
        for array in reversed(opened_arrays):
            close_array_memmap(array)


def _prepare_icp_job_input(
    runtime: Any,
    files: IcpJobFiles,
    preset: str,
    scale_refine_enabled: bool,
    solver_backend: str,
    backend_algorithm_version: str,
) -> tuple[IcpRuntimeInput, dict[str, Any]]:
    """실행용 NPY와 Small GICP 원본 분포 신뢰도 감사를 함께 준비합니다."""

    state = _icp_state(runtime)
    if state.phase != ICP_ALIGNMENT_PHASE_PREPARING:
        raise IcpRuntimeError("ICP job 입력은 준비 상태에서만 만들 수 있습니다.")
    seed = create_icp_seed(runtime)
    parameters = _create_parameters(preset, scale_refine_enabled)
    reference_model_ids = _reference_ids_for_seed(runtime, seed)
    model_state = _model_state_snapshot(
        runtime,
        (seed.moving_model_id, *reference_model_ids),
    )
    revisions = tuple(
        (model_id, revision)
        for model_id, revision, _display, _alignment in model_state
    )
    array_entries, confidence_preprocessing = _write_project_world_job_arrays(
        runtime,
        files,
        moving_model_id=seed.moving_model_id,
        reference_model_ids=reference_model_ids,
        moving_initial_alignment=Sim3Transform(
            matrix_float64=seed.initial_alignment_float64
        ),
        include_confidence=solver_backend == IcpSolverBackend.SMALL_GICP.value,
    )
    input_fingerprint, algorithm_version = _job_snapshot_fingerprint(
        files,
        seed=seed,
        parameters=parameters,
        reference_model_ids=reference_model_ids,
        model_revision_snapshot=revisions,
        array_entries=array_entries,
        moving_cloud_frame="INITIAL_PROJECT_WORLD",
    )
    return (
        IcpRuntimeInput(
            seed=seed,
            snapshot=None,
            parameters=parameters,
            input_fingerprint=input_fingerprint,
            model_state_snapshot=model_state,
            reference_model_ids=reference_model_ids,
            model_revision_snapshot=revisions,
            algorithm_version=algorithm_version,
            moving_cloud_frame="INITIAL_PROJECT_WORLD",
            solver_backend=solver_backend,
            backend_algorithm_version=backend_algorithm_version,
            confidence_preprocessing=confidence_preprocessing,
        ),
        array_entries,
    )


def _manifest_for_input(
    files: IcpJobFiles,
    input_data: IcpRuntimeInput,
    *,
    array_entries: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """경량 runtime 입력·job NPY·평가 전처리 감사로 manifest를 만듭니다."""

    if array_entries is None:
        array_entries, reference_ids = _array_manifest_entries(input_data, files)
    else:
        reference_ids = input_data.reference_model_ids
    marker_result = input_data.seed.marker_result
    if marker_result is None:
        marker_max_error = 0.0
        marker_rms = 0.0
        marker_reference_project = None
        moving_marker_display = None
    else:
        base_alignment = Sim3Transform(
            matrix_float64=input_data.seed.base_committed_alignment_float64
        )
        marker_max_error = marker_result.metrics.maximum
        marker_rms = marker_result.metrics.rms
        marker_reference_project = marker_result.representative_triangle.reference_vertices
        moving_marker_display = transform_positions(
            Sim3Transform(matrix_float64=base_alignment.inverse_float64),
            marker_result.representative_triangle.moving_vertices,
        )
    initial_alignment = Sim3Transform(
        matrix_float64=input_data.seed.initial_alignment_float64
    )
    payload = {
        "algorithm_version": input_data.algorithm_version,
        "backend_id": input_data.solver_backend,
        "backend_algorithm_version": input_data.backend_algorithm_version,
        "moving_cloud_frame": input_data.moving_cloud_frame,
        "parameters": input_data.parameters.to_mapping(),
        "moving_model_id": input_data.seed.moving_model_id,
        "reference_model_ids": list(reference_ids),
        "initial_alignment": initial_alignment.matrix_float64.tolist(),
        "marker_uniform_scale": float(initial_alignment.uniform_scale),
        "marker_max_error": float(marker_max_error),
        "marker_rms": float(marker_rms),
        "marker_reference_project": _json_value(marker_reference_project),
        "marker_moving_project": _json_value(moving_marker_display),
        "marker_signature": input_data.seed.marker_signature,
        "model_revision_snapshot": [list(item) for item in input_data.model_revision_snapshot],
        "model_state_snapshot": [list(item) for item in input_data.model_state_snapshot],
        "arrays": dict(array_entries),
    }
    if input_data.confidence_preprocessing:
        payload["confidence_preprocessing"] = _json_value(
            input_data.confidence_preprocessing
        )
    return build_manifest(
        job_id=files.job_directory.name,
        input_fingerprint=input_data.input_fingerprint,
        payload=payload,
    )


def _blender_binary_path(explicit_path: str | None) -> str:
    """명시 경로 또는 현재 bpy 실행 파일을 worker 실행 대상으로 반환합니다."""

    if explicit_path:
        return str(explicit_path)
    try:
        import bpy
    except ImportError as error:
        raise IcpRuntimeError("ICP worker를 시작할 Blender 실행 정보를 찾을 수 없습니다.") from error
    binary_path = str(getattr(bpy.app, "binary_path", "")).strip()
    if not binary_path:
        raise IcpRuntimeError("현재 Blender 실행 파일 경로가 비어 있습니다.")
    return binary_path


def begin_icp_job(
    runtime: Any,
    preset: str = "BALANCED",
    scale_refine_enabled: bool = False,
    blender_binary_path: str | None = None,
    solver_backend: IcpSolverBackend | str = IcpSolverBackend.CUSTOM_HYBRID,
    open3d_python_path: str | None = None,
) -> IcpJobHandle:
    """입력을 고정하고 선택한 외부 worker를 시작한 뒤 RUNNING 상태로 전이합니다."""

    state = _icp_state(runtime)
    if state.is_running or state.has_preview:
        raise IcpRuntimeError("현재 ICP 상태에서는 새 작업을 시작할 수 없습니다.")
    try:
        normalized_backend = normalize_solver_backend(solver_backend)
    except ValueError as error:
        raise IcpRuntimeError(str(error)) from error
    if is_external_python_backend(normalized_backend):
        if scale_refine_enabled and normalized_backend is not IcpSolverBackend.SMALL_GICP:
            raise IcpRuntimeError("선택한 외부 강체 ICP backend는 Scale Refine을 지원하지 않습니다.")
        if not str(open3d_python_path or "").strip():
            raise IcpRuntimeError(
                "외부 ICP 환경을 사용할 수 없습니다. CPython 3.12 worker 실행 경로를 설정하십시오."
            )
    backend_version = backend_algorithm_version(normalized_backend)
    state.phase = ICP_ALIGNMENT_PHASE_PREPARING
    state.last_error = ""
    state.started_at = monotonic()
    files: IcpJobFiles | None = None
    try:
        files = create_icp_job_directory()
        input_data, array_entries = _prepare_icp_job_input(
            runtime,
            files,
            preset,
            scale_refine_enabled,
            normalized_backend.value,
            backend_version,
        )
        manifest = _manifest_for_input(
            files,
            input_data,
            array_entries=array_entries,
        )
        write_job_manifest(files, manifest)
        if is_external_python_backend(normalized_backend):
            handle = launch_open3d_worker(
                files,
                str(open3d_python_path),
                input_data.input_fingerprint,
            )
        else:
            handle = launch_icp_worker(
                files,
                blender_binary_path=_blender_binary_path(blender_binary_path),
                input_fingerprint=input_data.input_fingerprint,
            )
    except (IcpBackendError, IcpJobProtocolError, IcpRuntimeError, MarkerAlignmentError, OSError, ValueError) as error:
        if files is not None:
            try:
                cleanup_unstarted_job_directory(files)
            except IcpBackendError as cleanup_error:
                _LOGGER.error(
                    "event=icp_job_prepare_cleanup_failed directory=%s reason=%s",
                    files.job_directory,
                    cleanup_error,
                )
        state.clear(error_message=str(error))
        state.phase = ICP_ALIGNMENT_PHASE_FAILED
        _LOGGER.warning("event=icp_job_failed phase=prepare reason=%s", error)
        raise IcpRuntimeError(str(error)) from error
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    state.input_data = input_data
    state.progress = IcpProgressState(
        stage=ICP_STAGE_PREPARING_INPUT,
        level_name="",
        iteration=0,
        progress=0.0,
        message="ICP 입력을 준비했습니다.",
        max_iterations=0,
        backend_id=normalized_backend.value,
        backend_algorithm_version=backend_version,
    )
    state.result = None
    state.working_alignment = None
    state.cancel_requested_at = None
    _LOGGER.info(
        "event=icp_job_started job_id=%s backend_id=%s moving_model_id=%s reference_model_count=%s input_fingerprint=%s",
        handle.job_id,
        normalized_backend.value,
        input_data.seed.moving_model_id,
        len(input_data.reference_model_ids),
        input_data.input_fingerprint,
    )
    return handle


def _scale_mode_name(parameters: Any) -> str:
    """core 설정의 scale mode를 비교 가능한 대문자 문자열로 반환합니다."""

    mode = getattr(parameters, "scale_mode", None)
    return str(getattr(mode, "value", mode)).strip().upper()


def _validate_stable_alignment(input_data: IcpRuntimeInput, alignment: Sim3Transform) -> None:
    """worker가 보낸 안정 transform이 현재 초기 정렬 scale 계약을 지키는지 검증합니다."""

    marker_scale = Sim3Transform(
        matrix_float64=input_data.seed.initial_alignment_float64
    ).uniform_scale
    scale_mode = _scale_mode_name(input_data.parameters)
    if scale_mode == "LOCKED":
        if not np.isclose(
            alignment.uniform_scale,
            marker_scale,
            rtol=0.0,
            atol=1.0e-9 * max(1.0, abs(marker_scale)),
        ):
            raise IcpJobProtocolError("고정 스케일 ICP 진행 transform이 초기 scale을 변경했습니다.")
        return
    if scale_mode == "REFINE":
        ratio = alignment.uniform_scale / marker_scale
        if not 0.95 - 1.0e-9 <= ratio <= 1.05 + 1.0e-9:
            raise IcpJobProtocolError("Scale Refine 진행 transform이 허용 범위를 벗어났습니다.")
        return
    raise IcpJobProtocolError("ICP 진행 transform의 scale mode가 올바르지 않습니다.")


def _validate_external_delta_scale(
    input_data: IcpRuntimeInput,
    uniform_scale: float,
) -> None:
    """공통 분해한 외부 ICP delta scale이 현재 backend 계약에 맞는지 검증합니다."""

    scale_mode = _scale_mode_name(input_data.parameters)
    if scale_mode == "LOCKED":
        if not np.isclose(uniform_scale, 1.0, rtol=0.0, atol=1.0e-9):
            raise IcpJobProtocolError("외부 ICP delta에 허용하지 않는 scale이 포함됐습니다.")
        return
    if scale_mode == "REFINE" and input_data.solver_backend == IcpSolverBackend.SMALL_GICP.value:
        maximum_change = float(input_data.parameters.scale_refine_max_relative_change)
        if 1.0 - maximum_change - 1.0e-9 <= uniform_scale <= 1.0 + maximum_change + 1.0e-9:
            return
        raise IcpJobProtocolError("small_gicp delta scale이 보정 범위를 벗어났습니다.")
    raise IcpJobProtocolError("외부 ICP 결과의 scale mode와 backend가 일치하지 않습니다.")


def _strict_json_integer(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """외부 JSON 정수를 문자열·bool·부동소수점 암묵 변환 없이 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise IcpJobProtocolError(f"{field_name}은(는) bool이 아닌 정수여야 합니다.")
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 정수로 변환할 수 없습니다.") from error
    if number < minimum or (maximum is not None and number > maximum):
        maximum_text = "" if maximum is None else f" {maximum} 이하"
        raise IcpJobProtocolError(f"{field_name}의 범위는 {minimum} 이상{maximum_text}이어야 합니다.")
    return number


def _strict_json_fraction(value: Any, *, field_name: str) -> float:
    """외부 JSON 진행률을 유한한 0 이상 1 이하 수치로 엄격 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise IcpJobProtocolError(f"{field_name}은(는) 숫자여야 합니다.")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 수치로 변환할 수 없습니다.") from error
    if not np.isfinite(number) or not 0.0 <= number <= 1.0:
        raise IcpJobProtocolError(f"{field_name}의 범위는 유한한 0 이상 1 이하여야 합니다.")
    return number


def _optional_progress_objective(value: Any) -> str | None:
    """선택 진행 목적함수를 공개 enum 문자열로 엄격히 정규화합니다."""

    if value is None:
        return None
    normalized = str(getattr(value, "value", value)).strip().upper()
    if normalized not in {"POINT_TO_POINT", "POINT_TO_PLANE"}:
        raise IcpJobProtocolError("ICP 진행 목적함수가 올바르지 않습니다.")
    return normalized


def _optional_progress_nonnegative_float(value: Any, *, field_name: str) -> float | None:
    """선택 진행 실수를 유한한 0 이상 값으로 엄격 검증합니다."""

    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise IcpJobProtocolError(f"{field_name}은(는) 숫자 또는 null이어야 합니다.")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 수치로 변환할 수 없습니다.") from error
    if not np.isfinite(number) or number < 0.0:
        raise IcpJobProtocolError(f"{field_name}은(는) 유한한 0 이상 값이어야 합니다.")
    return number


def _optional_progress_nonnegative_integer(value: Any, *, field_name: str) -> int | None:
    """선택 진행 개수를 bool·문자열 없이 0 이상 정수로 검증합니다."""

    if value is None:
        return None
    return _strict_json_integer(
        value,
        field_name=field_name,
        minimum=0,
        maximum=_MAX_EXTERNAL_JSON_INTEGER,
    )


def _maximum_progress_iteration(input_data: IcpRuntimeInput) -> int:
    """현재 ICP 설정에서 worker가 보고할 수 있는 반복 횟수의 상한을 계산합니다."""

    levels = tuple(getattr(input_data.parameters, "levels", ()))
    limits = [int(getattr(level, "max_iterations", 0)) for level in levels]
    limits.append(int(getattr(input_data.parameters, "scale_refine_max_iterations", 0)))
    return max(0, *limits)


def _progress_from_payload(
    payload: Mapping[str, Any],
    *,
    handle: IcpJobHandle,
    input_data: IcpRuntimeInput,
) -> IcpProgressState:
    """worker JSON 진행률을 UI가 안전하게 소비할 typed 상태로 검증합니다."""

    if str(payload.get("job_id", "")) != handle.job_id:
        raise IcpJobProtocolError("ICP worker 진행률 job_id가 현재 작업과 다릅니다.")
    if str(payload.get("input_fingerprint", "")) != input_data.input_fingerprint:
        raise IcpJobProtocolError("ICP worker 진행률 입력 fingerprint가 현재 작업과 다릅니다.")
    if str(payload.get("algorithm_version", "")) != input_data.algorithm_version:
        raise IcpJobProtocolError("ICP worker 진행률 알고리즘 버전이 현재 작업과 다릅니다.")
    backend_id = str(payload.get("backend_id", "CUSTOM_HYBRID")).strip().upper()
    if backend_id != input_data.solver_backend:
        raise IcpJobProtocolError("ICP worker 진행률 backend가 현재 작업과 다릅니다.")
    backend_algorithm_version = str(
        payload.get("backend_algorithm_version", input_data.backend_algorithm_version)
    ).strip()
    if backend_algorithm_version != input_data.backend_algorithm_version:
        raise IcpJobProtocolError("ICP worker 진행률 backend 알고리즘 버전이 현재 작업과 다릅니다.")
    if str(payload.get("moving_model_id", input_data.seed.moving_model_id)) != input_data.seed.moving_model_id:
        raise IcpJobProtocolError("ICP worker 진행률 이동 모델 ID가 현재 작업과 다릅니다.")
    stage = str(payload.get("stage", "")).strip()
    message = str(payload.get("message", "")).strip()
    level_name = str(payload.get("level_name", "")).strip()
    iteration = _strict_json_integer(
        payload.get("iteration", 0),
        field_name="ICP worker 진행 반복 횟수",
        minimum=0,
        maximum=_maximum_progress_iteration(input_data),
    )
    max_iterations = _strict_json_integer(
        payload.get("max_iterations", 0),
        field_name="ICP worker 진행 최대 반복 횟수",
        minimum=0,
        maximum=_maximum_progress_iteration(input_data),
    )
    if max_iterations and iteration > max_iterations:
        raise IcpJobProtocolError("ICP worker 진행 반복 횟수가 최대 반복 횟수를 넘었습니다.")
    progress = _strict_json_fraction(
        payload.get("progress", 0.0),
        field_name="ICP worker 진행률",
    )
    if backend_id == IcpSolverBackend.SMALL_GICP.value:
        valid_stages = _SMALL_GICP_PROGRESS_STAGES
    elif backend_id in _OPEN3D_BACKENDS:
        valid_stages = _OPEN3D_PROGRESS_STAGES
    else:
        valid_stages = _HYBRID_PROGRESS_STAGES
    if stage not in valid_stages:
        raise IcpJobProtocolError("ICP worker 진행 단계가 선택 backend 프로토콜에 없습니다.")
    objective_kind = _optional_progress_objective(payload.get("objective_kind"))
    maximum_correspondence_distance = _optional_progress_nonnegative_float(
        payload.get("maximum_correspondence_distance"),
        field_name="ICP worker 대응 반경",
    )
    correspondence_count = _optional_progress_nonnegative_integer(
        payload.get("correspondence_count"),
        field_name="ICP worker 대응점 수",
    )
    point_to_point_rmse = _optional_progress_nonnegative_float(
        payload.get("point_to_point_rmse"),
        field_name="ICP worker point RMSE",
    )
    point_to_plane_rmse = _optional_progress_nonnegative_float(
        payload.get("point_to_plane_rmse"),
        field_name="ICP worker plane RMSE",
    )
    normal_valid_count = _optional_progress_nonnegative_integer(
        payload.get("normal_valid_count"),
        field_name="ICP worker 유효 노멀 수",
    )
    normal_angle_pass_count = _optional_progress_nonnegative_integer(
        payload.get("normal_angle_pass_count"),
        field_name="ICP worker 노멀 각도 통과 수",
    )
    normal_radius = _optional_progress_nonnegative_float(
        payload.get("normal_radius"),
        field_name="ICP worker 실제 노멀 반경",
    )
    reference_sample_count = _optional_progress_nonnegative_integer(
        payload.get("reference_sample_count"),
        field_name="ICP worker 기준 표본 수",
    )
    moving_sample_count = _optional_progress_nonnegative_integer(
        payload.get("moving_sample_count"),
        field_name="ICP worker 이동 표본 수",
    )
    if (
        normal_valid_count is not None
        and normal_angle_pass_count is not None
        and normal_angle_pass_count > normal_valid_count
    ):
        raise IcpJobProtocolError("ICP worker 노멀 각도 통과 수가 유효 노멀 수를 넘습니다.")
    level_complete = payload.get("level_complete", False)
    if not isinstance(level_complete, (bool, np.bool_)):
        raise IcpJobProtocolError("ICP 진행률 level_complete는 bool이어야 합니다.")
    stable_alignment: Sim3Transform | None = None
    if "stable_alignment_4x4" in payload:
        try:
            matrix = np.asarray(payload["stable_alignment_4x4"], dtype=np.float64)
        except (OverflowError, TypeError, ValueError) as error:
            raise IcpJobProtocolError("ICP 진행률 stable alignment를 수치 행렬로 변환할 수 없습니다.") from error
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise IcpJobProtocolError("ICP 진행률 stable alignment가 올바르지 않습니다.")
        try:
            stable_alignment = Sim3Transform(matrix_float64=matrix)
            _validate_stable_alignment(input_data, stable_alignment)
        except (OverflowError, TypeError, ValueError) as error:
            raise IcpJobProtocolError("ICP 진행률 stable alignment가 올바르지 않습니다.") from error
    return IcpProgressState(
        stage=stage,
        level_name=level_name,
        iteration=iteration,
        progress=progress,
        message=message,
        stable_alignment=stable_alignment,
        max_iterations=max_iterations,
        level_complete=bool(level_complete),
        objective_kind=objective_kind,
        maximum_correspondence_distance=maximum_correspondence_distance,
        correspondence_count=correspondence_count,
        point_to_point_rmse=point_to_point_rmse,
        point_to_plane_rmse=point_to_plane_rmse,
        normal_valid_count=normal_valid_count,
        normal_angle_pass_count=normal_angle_pass_count,
        normal_radius=normal_radius,
        reference_sample_count=reference_sample_count,
        moving_sample_count=moving_sample_count,
        backend_id=backend_id,
        backend_algorithm_version=backend_algorithm_version,
        open3d_version=str(payload.get("open3d_version", "")).strip(),
        small_gicp_version=str(payload.get("small_gicp_version", "")).strip(),
    )


def _finalize_stopped_worker(
    state: IcpAlignmentRuntimeState,
    message: str,
    *,
    preserve_diagnostics: bool,
    failed: bool = True,
) -> None:
    """종료가 확인된 worker만 정리하고 marker preview 복귀 상태를 만듭니다."""

    handle = state.job_handle
    if handle is not None and not handle.is_running:
        try:
            cleanup_job_directory(handle, keep_failure_diagnostics=preserve_diagnostics)
        except IcpBackendError as error:
            message = f"{message} 작업 정리 실패: {error}"
        if not handle.cleaned and not any(
            retained is handle for retained in state.diagnostic_handles
        ):
            state.diagnostic_handles.append(handle)
    state.clear(error_message=message)
    if failed:
        state.phase = ICP_ALIGNMENT_PHASE_FAILED


def _stopped_worker_failure(
    state: IcpAlignmentRuntimeState,
    message: str,
) -> IcpRuntimeError:
    """종료가 확인된 worker의 손상 출력은 진단을 보존한 FAILED 상태로 정규화합니다."""

    handle = state.job_handle
    if handle is not None and handle.is_running:
        raise IcpRuntimeError("실행 중인 ICP worker를 종료 처리할 수 없습니다.")
    _finalize_stopped_worker(state, message, preserve_diagnostics=True)
    return IcpRuntimeError(state.last_error or message)


def _cleanup_retained_diagnostics(state: IcpAlignmentRuntimeState) -> None:
    """세션 수명주기 종료 시 보관했던 실패 작업 디렉터리를 명시적으로 제거합니다."""

    retained: list[IcpJobHandle] = []
    failures: list[str] = []
    for handle in state.diagnostic_handles:
        if handle.is_running:
            retained.append(handle)
            failures.append(f"{handle.job_id}: 아직 실행 중입니다.")
            continue
        try:
            cleanup_job_directory(handle, keep_failure_diagnostics=False)
        except IcpBackendError as error:
            retained.append(handle)
            failures.append(f"{handle.job_id}: {error}")
    state.diagnostic_handles[:] = retained
    if failures:
        raise IcpRuntimeError("ICP 실패 진단 디렉터리를 정리하지 못했습니다. " + " / ".join(failures))


def _begin_worker_failure_shutdown(state: IcpAlignmentRuntimeState, message: str) -> None:
    """실행 중 worker handle을 보존한 채 취소·종료 대기 상태로 전이합니다."""

    handle = state.job_handle
    if handle is None:
        state.clear(error_message=message)
        state.phase = ICP_ALIGNMENT_PHASE_FAILED
        return
    state.last_error = message
    if handle.is_running:
        request_job_cancel(handle)
        state.phase = ICP_ALIGNMENT_PHASE_CANCEL_PENDING
        state.cancel_requested_at = monotonic()
        state.termination_requested_at = None
        return
    _finalize_stopped_worker(state, message, preserve_diagnostics=True)


def _validate_finite_json_values(value: Any, *, field_name: str) -> None:
    """품질 JSON의 모든 수치가 유한하고 지원 타입인지 재귀 검증합니다."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not np.isfinite(value):
            raise IcpJobProtocolError(f"{field_name}에 유한하지 않은 수치가 있습니다.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_finite_json_values(item, field_name=f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_finite_json_values(item, field_name=f"{field_name}.{key}")
        return
    raise IcpJobProtocolError(f"{field_name}에 지원하지 않는 JSON 값이 있습니다.")


def _external_runtime_result_from_worker(
    runtime: Any,
    state: IcpAlignmentRuntimeState,
    metadata: Mapping[str, Any],
    arrays: Mapping[str, np.ndarray],
) -> IcpRuntimeResult:
    """외부 ICP 결과를 기존 미리보기·승인 Runtime 표현으로 엄격히 정규화합니다."""

    input_data = state.input_data
    if input_data is None:
        raise IcpRuntimeError("외부 ICP 결과에 연결된 입력 snapshot이 없습니다.")
    from .icp_backend import validate_result_metadata_for_backend

    validate_result_metadata_for_backend(metadata, input_data.solver_backend)
    if str(metadata.get("algorithm_version", "")) != input_data.algorithm_version:
        raise IcpJobProtocolError("외부 ICP 결과 core 입력 버전이 현재 작업과 다릅니다.")
    if str(metadata.get("backend_algorithm_version", "")) != input_data.backend_algorithm_version:
        raise IcpJobProtocolError("외부 ICP 결과 backend 알고리즘 버전이 현재 작업과 다릅니다.")
    quality_payload = metadata.get("quality")
    if not isinstance(quality_payload, Mapping):
        raise IcpJobProtocolError("외부 ICP 결과 품질 형식이 올바르지 않습니다.")
    _validate_finite_json_values(quality_payload, field_name="외부 ICP worker 품질")
    quality_status = str(quality_payload.get("status", "")).strip().upper()
    if quality_status not in {"PASS", "REVIEW"}:
        raise IcpJobProtocolError("외부 ICP 결과 품질은 PASS 또는 REVIEW여야 합니다.")
    expected_parameter_fingerprint = str(
        getattr(input_data.parameters, "fingerprint", "")
    ).strip()
    actual_parameter_fingerprint = str(metadata.get("parameter_fingerprint", "")).strip()
    if actual_parameter_fingerprint != expected_parameter_fingerprint:
        raise IcpRuntimeStaleError("외부 ICP 결과 parameter fingerprint가 현재 입력과 다릅니다.")
    if str(metadata.get("scale_mode", "")).strip().upper() != _scale_mode_name(
        input_data.parameters
    ):
        raise IcpJobProtocolError("외부 ICP 결과 scale mode가 현재 입력과 다릅니다.")
    if {
        "initial_alignment_4x4",
        "final_alignment_4x4",
        "icp_delta_4x4",
    }.intersection(metadata):
        raise IcpJobProtocolError("외부 ICP 결과 JSON에는 변환 행렬을 기록할 수 없습니다.")
    if set(arrays) != {"initial_alignment", "final_alignment", "icp_delta"}:
        raise IcpJobProtocolError("외부 ICP 결과 NPZ 행렬 항목이 고정 계약과 다릅니다.")
    try:
        initial_alignment = Sim3Transform(matrix_float64=arrays["initial_alignment"])
        final_alignment = Sim3Transform(matrix_float64=arrays["final_alignment"])
        icp_delta = Sim3Transform(matrix_float64=arrays["icp_delta"])
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError("외부 ICP 결과 변환 행렬이 올바르지 않습니다.") from error
    if not np.allclose(
        initial_alignment.matrix_float64,
        input_data.seed.initial_alignment_float64,
        rtol=0.0,
        atol=1.0e-8,
    ):
        raise IcpRuntimeStaleError("외부 ICP 결과 초기 alignment가 실행 seed와 다릅니다.")
    _validate_stable_alignment(input_data, final_alignment)
    expected_final = icp_delta.matrix_float64 @ input_data.seed.initial_alignment_float64
    if not np.allclose(final_alignment.matrix_float64, expected_final, rtol=0.0, atol=1.0e-8):
        raise IcpJobProtocolError("외부 ICP final alignment가 delta × 초기 alignment와 다릅니다.")
    try:
        local_origin = np.asarray(metadata["local_origin"], dtype=np.float64)
        moving_diagonal = float(metadata["moving_diagonal"])
        reported_delta_translation = float(metadata["delta_translation"])
        reported_delta_rotation = float(metadata["delta_rotation_degrees"])
    except (KeyError, OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError("외부 ICP 결과 local-frame 진단값이 올바르지 않습니다.") from error
    if local_origin.shape != (3,) or not np.isfinite(local_origin).all() or not np.isfinite(moving_diagonal) or moving_diagonal <= 0.0:
        raise IcpJobProtocolError("외부 ICP 결과 local-frame 진단값이 올바르지 않습니다.")
    try:
        delta_diagnostics = analyze_external_icp_delta(
            arrays["icp_delta"],
            local_origin,
            field_name="외부 ICP 결과 NPZ icp_delta",
        )
    except Open3dIcpError as error:
        raise IcpJobProtocolError("외부 ICP 결과 delta 행렬 진단값이 올바르지 않습니다.") from error
    _validate_external_delta_scale(input_data, delta_diagnostics.uniform_scale)
    delta_translation = delta_diagnostics.translation_distance
    delta_rotation = delta_diagnostics.rotation_degrees
    if not np.isclose(delta_translation, reported_delta_translation, rtol=0.0, atol=1.0e-8):
        raise IcpJobProtocolError("외부 ICP 결과 추가 이동량이 delta 행렬과 다릅니다.")
    if not np.isclose(delta_rotation, reported_delta_rotation, rtol=0.0, atol=1.0e-8):
        raise IcpJobProtocolError("외부 ICP 결과 추가 회전량이 delta 행렬과 다릅니다.")
    if delta_rotation > float(input_data.parameters.max_rotation_degrees) + 1.0e-8:
        raise IcpJobProtocolError("외부 ICP 결과 추가 회전이 안전 한도를 넘었습니다.")
    maximum_translation = moving_diagonal * float(input_data.parameters.max_translation_fraction)
    if delta_translation > maximum_translation + 1.0e-8:
        raise IcpJobProtocolError("외부 ICP 결과 추가 이동이 안전 한도를 넘었습니다.")
    if str(metadata.get("moving_model_id", "")) != input_data.seed.moving_model_id:
        raise IcpRuntimeStaleError("외부 ICP 결과 이동 모델이 현재 작업과 다릅니다.")
    raw_reference_ids = metadata.get("reference_model_ids")
    if not isinstance(raw_reference_ids, list):
        raise IcpJobProtocolError("외부 ICP 결과 기준 A 모델 목록이 올바르지 않습니다.")
    reference_ids = tuple(str(item).strip() for item in raw_reference_ids)
    expected_positions = {
        model_id: index for index, model_id in enumerate(input_data.reference_model_ids)
    }
    if (
        not reference_ids
        or len(set(reference_ids)) != len(reference_ids)
        or any(model_id not in expected_positions for model_id in reference_ids)
        or tuple(expected_positions[model_id] for model_id in reference_ids)
        != tuple(sorted(expected_positions[model_id] for model_id in reference_ids))
    ):
        raise IcpRuntimeStaleError(
            "외부 ICP 결과 기준 A 모델은 입력 A 모델의 순서 보존 부분집합이어야 합니다."
        )
    raw_revisions = metadata.get("model_revision_snapshot")
    if not isinstance(raw_revisions, list) or any(
        not isinstance(item, (list, tuple)) or len(item) != 2 for item in raw_revisions
    ):
        raise IcpJobProtocolError("외부 ICP 결과 모델 revision snapshot이 올바르지 않습니다.")
    revisions = tuple(
        sorted(
            (
                str(item[0]),
                _strict_json_integer(
                    item[1],
                    field_name=f"외부 ICP 결과 모델 revision[{index}]",
                    minimum=1,
                    maximum=_MAX_EXTERNAL_JSON_INTEGER,
                ),
            )
            for index, item in enumerate(raw_revisions)
        )
    )
    if revisions != input_data.model_revision_snapshot:
        raise IcpRuntimeStaleError("외부 ICP 결과 모델 revision이 현재 입력 snapshot과 다릅니다.")
    raw_model_state = metadata.get("model_state_snapshot")
    if not isinstance(raw_model_state, list) or any(
        not isinstance(item, (list, tuple)) or len(item) != 4 for item in raw_model_state
    ):
        raise IcpJobProtocolError("외부 ICP 결과 모델 상태 snapshot이 올바르지 않습니다.")
    result_model_state = tuple(
        (
            str(item[0]),
            _strict_json_integer(
                item[1],
                field_name=f"외부 ICP 결과 모델 상태 revision[{index}]",
                minimum=1,
                maximum=_MAX_EXTERNAL_JSON_INTEGER,
            ),
            str(item[2]),
            str(item[3]),
        )
        for index, item in enumerate(raw_model_state)
    )
    if result_model_state != input_data.model_state_snapshot:
        raise IcpRuntimeStaleError("외부 ICP 결과 모델 상태가 현재 입력과 다릅니다.")
    if not _current_input_matches(runtime, input_data):
        raise IcpRuntimeStaleError("외부 ICP 작업 중 모델 또는 marker 입력이 변경됐습니다.")
    return IcpRuntimeResult(
        moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=reference_ids,
        final_alignment=final_alignment,
        icp_delta=icp_delta,
        quality=dict(quality_payload),
        metadata=dict(metadata),
        input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=actual_parameter_fingerprint,
        marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=revisions,
        algorithm_version=input_data.algorithm_version,
        solver_backend=input_data.solver_backend,
        backend_algorithm_version=input_data.backend_algorithm_version,
    )


def _runtime_result_from_worker(
    runtime: Any,
    state: IcpAlignmentRuntimeState,
    metadata: Mapping[str, Any],
    arrays: Mapping[str, np.ndarray],
) -> IcpRuntimeResult:
    """worker 결과 파일의 식별자·행렬·품질을 재검증해 runtime 결과를 만듭니다."""

    input_data = state.input_data
    handle = state.job_handle
    if input_data is None or handle is None:
        raise IcpRuntimeError("ICP worker 결과에 연결된 입력 snapshot이 없습니다.")
    if str(metadata.get("job_id", "")) != handle.job_id:
        raise IcpRuntimeStaleError("ICP worker 결과 job_id가 현재 작업과 다릅니다.")
    if str(metadata.get("input_fingerprint", "")) != input_data.input_fingerprint:
        raise IcpRuntimeStaleError("ICP worker 결과 입력 fingerprint가 현재 작업과 다릅니다.")
    if str(metadata.get("marker_signature", "")) != input_data.seed.marker_signature:
        raise IcpRuntimeStaleError("ICP worker 결과 marker signature가 현재 입력과 다릅니다.")
    if str(metadata.get("algorithm_version", "")) != input_data.algorithm_version:
        raise IcpJobProtocolError("ICP worker 결과 알고리즘 버전이 현재 구현과 다릅니다.")
    if input_data.solver_backend in _EXTERNAL_PYTHON_BACKENDS:
        return _external_runtime_result_from_worker(runtime, state, metadata, arrays)
    validate_hybrid_result_metadata(metadata)
    quality_payload = metadata.get("quality")
    if not isinstance(quality_payload, Mapping):
        raise IcpJobProtocolError("ICP worker 결과 품질 형식이 올바르지 않습니다.")
    _validate_finite_json_values(quality_payload, field_name="ICP worker 품질")
    expected_parameter_fingerprint = str(
        getattr(input_data.parameters, "fingerprint", "")
    ).strip()
    actual_parameter_fingerprint = str(metadata.get("parameter_fingerprint", "")).strip()
    if not expected_parameter_fingerprint or actual_parameter_fingerprint != expected_parameter_fingerprint:
        raise IcpRuntimeStaleError("ICP worker 결과 parameter fingerprint가 현재 입력과 다릅니다.")
    matrix_metadata_keys = {
        "initial_alignment_4x4",
        "final_alignment_4x4",
        "icp_delta_4x4",
    }
    if matrix_metadata_keys.intersection(metadata):
        raise IcpJobProtocolError("ICP 결과 JSON에는 변환 행렬을 기록할 수 없습니다.")
    expected_array_names = {"initial_alignment", "final_alignment", "icp_delta"}
    if set(arrays) != expected_array_names:
        raise IcpJobProtocolError("ICP 결과 NPZ 행렬 항목이 고정 계약과 다릅니다.")
    raw_revisions = metadata.get("model_revision_snapshot")
    if not isinstance(raw_revisions, list) or any(
        not isinstance(item, (list, tuple)) or len(item) != 2 for item in raw_revisions
    ):
        raise IcpJobProtocolError("ICP worker 결과 모델 revision snapshot이 올바르지 않습니다.")
    for index, item in enumerate(raw_revisions):
        _strict_json_integer(
            item[1],
            field_name=f"ICP worker 결과 모델 revision[{index}]",
            minimum=1,
            maximum=_MAX_EXTERNAL_JSON_INTEGER,
        )
    raw_model_state = metadata.get("model_state_snapshot")
    if not isinstance(raw_model_state, list) or any(
        not isinstance(item, (list, tuple)) or len(item) != 4 for item in raw_model_state
    ):
        raise IcpJobProtocolError("ICP worker 결과 모델 상태 snapshot이 올바르지 않습니다.")
    result_model_state = tuple(
        (
            str(item[0]),
            _strict_json_integer(
                item[1],
                field_name=f"ICP worker 결과 모델 상태 revision[{index}]",
                minimum=1,
                maximum=_MAX_EXTERNAL_JSON_INTEGER,
            ),
            str(item[2]),
            str(item[3]),
        )
        for index, item in enumerate(raw_model_state)
    )
    try:
        from .icp_types import IcpAlignmentResult, IcpError
    except ImportError as error:
        raise IcpRuntimeError("ICP 결과 타입을 불러오지 못했습니다.") from error
    core_mapping = dict(metadata)
    core_mapping["initial_alignment_4x4"] = arrays["initial_alignment"]
    core_mapping["final_alignment_4x4"] = arrays["final_alignment"]
    core_mapping["icp_delta_4x4"] = arrays["icp_delta"]
    try:
        core_result = IcpAlignmentResult.from_mapping(core_mapping)
    except (IcpError, KeyError, OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError("ICP worker 결과 형식을 복원하지 못했습니다.") from error
    if core_result.moving_model_id != input_data.seed.moving_model_id:
        raise IcpRuntimeStaleError("ICP worker 결과 이동 모델이 현재 작업과 다릅니다.")
    if core_result.algorithm_version != input_data.algorithm_version:
        raise IcpJobProtocolError("복원한 ICP 결과 알고리즘 버전이 현재 입력과 다릅니다.")
    quality_status = str(
        getattr(core_result.quality.status, "value", core_result.quality.status)
    ).upper()
    if quality_status not in {"PASS", "REVIEW"}:
        raise IcpJobProtocolError("Hybrid ICP 결과 품질은 PASS 또는 REVIEW여야 합니다.")
    initial_alignment = core_result.initial_alignment
    final_alignment = core_result.final_alignment
    icp_delta = core_result.icp_delta
    if not np.allclose(
        initial_alignment.matrix_float64,
        input_data.seed.initial_alignment_float64,
        rtol=0.0,
        atol=1.0e-8,
    ):
        raise IcpRuntimeStaleError("ICP worker 결과 초기 alignment가 실행 seed와 다릅니다.")
    _validate_stable_alignment(input_data, final_alignment)
    expected_final = icp_delta.matrix_float64 @ input_data.seed.initial_alignment_float64
    if not np.allclose(final_alignment.matrix_float64, expected_final, rtol=0.0, atol=1.0e-8):
        raise IcpJobProtocolError("ICP worker final alignment가 delta × 초기 alignment와 다릅니다.")
    if not isinstance(metadata.get("reference_model_ids"), list):
        raise IcpJobProtocolError("ICP worker 결과 기준 A 모델 목록이 올바르지 않습니다.")
    reference_ids = core_result.reference_model_ids
    expected_reference_ids = input_data.reference_model_ids
    expected_positions = {
        model_id: index for index, model_id in enumerate(expected_reference_ids)
    }
    if (
        not reference_ids
        or len(set(reference_ids)) != len(reference_ids)
        or any(model_id not in expected_positions for model_id in reference_ids)
        or tuple(expected_positions[model_id] for model_id in reference_ids)
        != tuple(sorted(expected_positions[model_id] for model_id in reference_ids))
    ):
        raise IcpRuntimeStaleError(
            "ICP worker 결과 기준 A 모델은 입력 A 모델의 순서 보존 부분집합이어야 합니다."
        )
    revisions = core_result.model_revision_snapshot
    if revisions != input_data.model_revision_snapshot:
        raise IcpRuntimeStaleError("ICP worker 결과 모델 revision이 현재 입력 snapshot과 다릅니다.")
    if result_model_state != input_data.model_state_snapshot:
        raise IcpRuntimeStaleError("ICP worker 결과 모델 상태 snapshot이 현재 입력과 다릅니다.")
    if not _current_input_matches(runtime, input_data):
        raise IcpRuntimeStaleError("ICP 작업 중 모델 또는 marker 입력이 변경되어 결과를 미리보기로 표시할 수 없습니다.")
    return IcpRuntimeResult(
        moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=reference_ids,
        final_alignment=final_alignment,
        icp_delta=icp_delta,
        quality=core_result.quality.to_mapping(),
        metadata=dict(metadata),
        input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=actual_parameter_fingerprint,
        marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=revisions,
        algorithm_version=core_result.algorithm_version,
        raw_result=core_result,
        solver_backend=input_data.solver_backend,
        backend_algorithm_version=input_data.backend_algorithm_version,
    )


def _worker_error_message(
    handle: IcpJobHandle,
    input_data: IcpRuntimeInput | None,
    error_data: Mapping[str, Any] | None,
    fallback: str,
) -> str:
    """식별자가 맞는 worker 오류만 사용자에게 전달할 실패 문장으로 정리합니다."""

    if not isinstance(error_data, Mapping) or input_data is None:
        return fallback
    if str(error_data.get("job_id", "")) != handle.job_id:
        return "ICP worker 오류 job_id가 현재 작업과 다릅니다."
    if str(error_data.get("input_fingerprint", "")) != input_data.input_fingerprint:
        return "ICP worker 오류 입력 fingerprint가 현재 작업과 다릅니다."
    message = str(error_data.get("message", "")).strip() or fallback
    if str(error_data.get("status", "")).upper() != "QUALITY_REJECTED":
        return message
    raw_reasons = error_data.get("rejection_reasons", error_data.get("review_reasons", ()))
    if not isinstance(raw_reasons, list):
        return "ICP 품질 거부 오류 형식이 올바르지 않습니다."
    reasons = [str(reason).strip() for reason in raw_reasons if str(reason).strip()]
    suffix = " / ".join(reasons)
    return f"ICP 품질 거부: {message}" + (f" ({suffix})" if suffix else "")


def _read_terminal_progress(
    state: IcpAlignmentRuntimeState,
    handle: IcpJobHandle,
    input_data: IcpRuntimeInput,
) -> None:
    """성공 worker가 남긴 최종 진행률을 다시 검증해 손상된 완료 출력을 거부합니다."""

    payload = read_job_progress(handle)
    if payload is None:
        raise IcpJobProtocolError("정상 종료된 ICP worker의 진행률 파일이 없습니다.")
    progress = _progress_from_payload(payload, handle=handle, input_data=input_data)
    state.progress = progress
    if progress.stable_alignment is not None:
        state.working_alignment = progress.stable_alignment


def _validate_terminal_progress_for_result(
    progress: IcpProgressState | None,
    result: IcpRuntimeResult,
) -> None:
    """완료 progress가 PASS·REVIEW 결과와 같은 최선 후보를 가리키는지 검증합니다."""

    if progress is None:
        raise IcpJobProtocolError("정상 종료된 ICP worker의 진행률 상태가 없습니다.")
    if result.solver_backend == IcpSolverBackend.SMALL_GICP.value:
        expected_stage = (
            "SMALL_GICP_COMPLETED_PASS"
            if result.quality_status == "PASS"
            else "SMALL_GICP_COMPLETED_REVIEW"
        )
    elif result.solver_backend in _OPEN3D_BACKENDS:
        expected_stage = (
            "OPEN3D_COMPLETED_PASS"
            if result.quality_status == "PASS"
            else "OPEN3D_COMPLETED_REVIEW"
        )
    else:
        expected_stage = (
            ICP_STAGE_COMPLETED_PASS
            if result.quality_status == "PASS"
            else ICP_STAGE_COMPLETED_REVIEW
        )
    if progress.stage != expected_stage or progress.progress != 1.0:
        raise IcpJobProtocolError("ICP 완료 진행 단계가 결과 품질 상태와 다릅니다.")
    if progress.stable_alignment is None:
        raise IcpJobProtocolError("ICP 완료 진행률에 최선 후보 transform이 없습니다.")
    if not np.allclose(
        progress.stable_alignment.matrix_float64,
        result.final_alignment.matrix_float64,
        rtol=0.0,
        atol=1.0e-8,
    ):
        raise IcpJobProtocolError("ICP 완료 진행률 transform이 최종 최선 후보와 다릅니다.")


def finish_icp_job(runtime: Any) -> IcpRuntimeResult:
    """정상 종료한 worker 결과를 검증해 ICP 비파괴 preview 상태로 전이합니다."""

    state = _icp_state(runtime)
    handle = state.job_handle
    if state.phase not in {ICP_ALIGNMENT_PHASE_RUNNING, ICP_ALIGNMENT_PHASE_CANCEL_PENDING} or handle is None:
        raise IcpRuntimeError("완료 처리할 ICP 작업이 없습니다.")
    if handle.is_running:
        raise IcpRuntimeError("실행 중인 ICP worker 결과를 완료 처리할 수 없습니다.")
    try:
        return_code = wait_for_job_worker(handle, timeout_seconds=0.0)
    except IcpBackendError as error:
        failure = _stopped_worker_failure(
            state,
            f"종료된 ICP worker의 종료 상태를 확인하지 못했습니다: {error}",
        )
        raise failure from error
    if return_code is None:
        raise IcpRuntimeError("종료된 ICP worker의 종료 코드를 확인하지 못했습니다.")
    if return_code != 0:
        try:
            error_data = read_job_error(handle)
        except IcpBackendError as error:
            failure = _stopped_worker_failure(
                state,
                f"비정상 종료된 ICP worker의 오류 파일을 읽지 못했습니다: {error}",
            )
            raise failure from error
        message = _worker_error_message(
            handle,
            state.input_data,
            error_data,
            f"ICP worker가 비정상 종료했습니다. 종료 코드: {return_code}",
        )
        raise _stopped_worker_failure(state, message)
    try:
        result_data = read_job_result(handle)
    except IcpBackendError as error:
        failure = _stopped_worker_failure(
            state,
            f"정상 종료된 ICP worker의 결과 파일을 읽지 못했습니다: {error}",
        )
        raise failure from error
    if result_data is None:
        try:
            error_data = read_job_error(handle)
        except IcpBackendError as error:
            failure = _stopped_worker_failure(
                state,
                f"결과 없이 종료된 ICP worker의 오류 파일을 읽지 못했습니다: {error}",
            )
            raise failure from error
        message = _worker_error_message(
            handle,
            state.input_data,
            error_data,
            "ICP worker가 결과를 기록하지 않았습니다.",
        )
        raise _stopped_worker_failure(state, message)
    metadata, arrays = result_data
    try:
        input_data = state.input_data
        if input_data is None:
            raise IcpRuntimeError("ICP 결과에 연결된 입력 snapshot이 없습니다.")
        _read_terminal_progress(state, handle, input_data)
        result = _runtime_result_from_worker(runtime, state, metadata, arrays)
        _validate_terminal_progress_for_result(state.progress, result)
    except (
        IcpBackendError,
        IcpError,
        IcpRuntimeError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        failure = _stopped_worker_failure(state, str(error))
        raise failure from error
    try:
        cleanup_job_directory(handle, keep_failure_diagnostics=False)
    except IcpBackendError as error:
        failure = _stopped_worker_failure(state, str(error))
        raise failure from error
    state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
    state.job_handle = None
    state.result = result
    state.working_alignment = result.final_alignment
    state.cancel_requested_at = None
    state.termination_requested_at = None
    _LOGGER.info(
        "event=icp_preview_registered moving_model_id=%s input_fingerprint=%s",
        result.moving_model_id,
        result.input_fingerprint,
    )
    return result


def request_icp_cancel(runtime: Any) -> bool:
    """실행 중인 ICP worker에 취소 sentinel을 보내고 취소 대기 상태로 전이합니다."""

    state = _icp_state(runtime)
    handle = state.job_handle
    if state.phase not in {ICP_ALIGNMENT_PHASE_RUNNING, ICP_ALIGNMENT_PHASE_CANCEL_PENDING} or handle is None:
        return False
    if state.phase == ICP_ALIGNMENT_PHASE_CANCEL_PENDING:
        return False
    if not request_job_cancel(handle):
        return False
    state.phase = ICP_ALIGNMENT_PHASE_CANCEL_PENDING
    state.cancel_requested_at = monotonic()
    state.termination_requested_at = None
    _LOGGER.info("event=icp_job_cancel_requested job_id=%s", handle.job_id)
    return True


def poll_icp_job(runtime: Any) -> IcpProgressState | None:
    """Timer가 worker 진행·종료·취소를 처리하고 main 상태만 갱신합니다."""

    state = _icp_state(runtime)
    handle = state.job_handle
    if state.phase not in {ICP_ALIGNMENT_PHASE_RUNNING, ICP_ALIGNMENT_PHASE_CANCEL_PENDING} or handle is None:
        return state.progress
    try:
        if state.phase == ICP_ALIGNMENT_PHASE_RUNNING:
            if state.input_data is None:
                raise IcpRuntimeError("ICP 진행률에 연결된 입력 snapshot이 없습니다.")
            if not _current_input_matches(runtime, state.input_data):
                raise IcpRuntimeStaleError(
                    "ICP 작업 중 모델 또는 marker 입력이 변경되어 worker를 취소합니다."
                )
            progress_payload = read_job_progress(handle)
            if progress_payload is not None:
                progress = _progress_from_payload(
                    progress_payload,
                    handle=handle,
                    input_data=state.input_data,
                )
                state.progress = progress
                if progress.stable_alignment is not None:
                    state.working_alignment = progress.stable_alignment
    except (
        IcpBackendError,
        IcpError,
        IcpRuntimeError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        _begin_worker_failure_shutdown(state, str(error))
        return state.progress
    if handle.is_running:
        if state.phase == ICP_ALIGNMENT_PHASE_CANCEL_PENDING:
            now = monotonic()
            if (
                state.termination_requested_at is None
                and state.cancel_requested_at is not None
                and now - state.cancel_requested_at >= ICP_CANCEL_GRACE_SECONDS
            ):
                try:
                    if terminate_job_worker(handle):
                        state.termination_requested_at = now
                except IcpBackendError as error:
                    state.last_error = f"ICP worker 종료 요청에 실패했습니다: {error}"
                    return state.progress
            elif (
                state.termination_requested_at is not None
                and now - state.termination_requested_at >= ICP_CANCEL_GRACE_SECONDS
            ):
                try:
                    if kill_job_worker(handle):
                        wait_for_job_worker(handle, timeout_seconds=0.0)
                except IcpBackendError as error:
                    state.last_error = f"ICP worker 강제 종료에 실패했습니다: {error}"
                    return state.progress
        return state.progress
    try:
        return_code = wait_for_job_worker(handle, timeout_seconds=0.0)
    except IcpBackendError as error:
        _stopped_worker_failure(
            state,
            f"종료된 ICP worker의 종료 상태를 확인하지 못했습니다: {error}",
        )
        return None
    if return_code is None:
        _stopped_worker_failure(
            state,
            "종료된 ICP worker의 종료 코드를 확인하지 못했습니다.",
        )
        return None
    if state.phase == ICP_ALIGNMENT_PHASE_CANCEL_PENDING:
        message = state.last_error or "ICP 작업을 취소했습니다."
        _finalize_stopped_worker(
            state,
            message,
            preserve_diagnostics=bool(state.last_error),
            failed=bool(state.last_error),
        )
        _LOGGER.info("event=icp_job_cancelled job_id=%s", handle.job_id)
        return None
    if return_code != 0:
        try:
            error_data = read_job_error(handle)
        except IcpBackendError as error:
            _stopped_worker_failure(
                state,
                f"비정상 종료된 ICP worker의 오류 파일을 읽지 못했습니다: {error}",
            )
            return None
        message = _worker_error_message(
            handle,
            state.input_data,
            error_data,
            f"ICP worker가 비정상 종료했습니다. 종료 코드: {return_code}",
        )
        _finalize_stopped_worker(state, message, preserve_diagnostics=True)
        return None
    try:
        finish_icp_job(runtime)
    except IcpRuntimeError:
        # 완료 처리 함수가 종료 worker를 FAILED로 정규화했으므로 timer에는 예외를 남기지 않습니다.
        return None
    return state.progress


def discard_icp_preview(runtime: Any) -> None:
    """Repository를 바꾸지 않고 ICP 결과만 버려 실행 전 초기 정렬로 돌아갑니다."""

    state = _icp_state(runtime)
    if not state.has_preview:
        raise IcpRuntimeError("버릴 ICP 결과 미리보기가 없습니다.")
    result = state.result
    state.clear()
    _LOGGER.info(
        "event=icp_result_discarded moving_model_id=%s",
        result.moving_model_id if result is not None else "",
    )


def _marker_preview_snapshot(state: MarkerAlignmentPreviewState) -> IcpMarkerPreviewSnapshot:
    """승인 전 marker preview를 Undo 복원용 값 객체로 복사합니다."""

    return IcpMarkerPreviewSnapshot(
        phase=state.phase,
        result=state.result,
        last_error=state.last_error,
        started_at=state.started_at,
        p_view_snapshot=state.p_view_snapshot,
    )


def _restore_marker_preview_snapshot(
    state: MarkerAlignmentPreviewState,
    snapshot: IcpMarkerPreviewSnapshot,
) -> None:
    """검증된 ICP Undo snapshot의 marker preview만 원자 복원합니다."""

    state.phase = snapshot.phase
    state.result = snapshot.result
    state.last_error = snapshot.last_error
    state.started_at = snapshot.started_at
    state.p_view_snapshot = snapshot.p_view_snapshot


def _result_fingerprint(result: IcpRuntimeResult) -> str:
    """Undo 이력과 stale 진단에 사용할 ICP 결과 행렬 지문을 계산합니다."""

    digest = hashlib.blake2b(digest_size=20)
    digest.update(result.input_fingerprint.encode("ascii"))
    digest.update(b"\0")
    digest.update(result.final_alignment.fingerprint.encode("ascii"))
    return digest.hexdigest()


def _current_input_matches(runtime: Any, input_data: IcpRuntimeInput) -> bool:
    """승인 직전에 worker 시작 시점의 모델·축·확정 alignment snapshot을 다시 확인합니다."""

    if not icp_seed_is_current(runtime, input_data.seed):
        return False
    try:
        current = _model_state_snapshot(
            runtime,
            tuple(item[0] for item in input_data.model_state_snapshot),
        )
    except (IcpRuntimeError, ModelRepositoryError):
        return False
    return current == input_data.model_state_snapshot


def _promote_catalog_for_icp(runtime: Any, moving_model_id: str) -> None:
    """일반 마커 잠금을 우회하지 않고 전용 승인 경계에서만 P를 A로 편입합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    promoter = getattr(catalog, "promote_model_for_icp_commit", None)
    if not callable(promoter):
        raise IcpRuntimeError("ICP 승인용 Data Layers 편입 기능이 없습니다.")
    promoter(moving_model_id)


def _icp_provenance_record(
    input_data: IcpRuntimeInput,
    result: IcpRuntimeResult,
) -> AlignmentProvenanceRecord:
    """승인할 ICP 결과의 작은 출처·품질 감사 기록을 생성합니다."""

    marker_result = input_data.seed.marker_result
    quality = result.quality
    fitness_value = quality.get("fitness")
    rmse_value = quality.get("rmse")
    return AlignmentProvenanceRecord(
        model_id=result.moving_model_id,
        method="ICP",
        backend_id=result.solver_backend,
        algorithm_version=result.backend_algorithm_version,
        input_fingerprint=result.input_fingerprint,
        result_fingerprint=_result_fingerprint(result),
        marker_rms=(marker_result.metrics.rms if marker_result is not None else None),
        marker_maximum_error=(
            marker_result.metrics.maximum if marker_result is not None else None
        ),
        icp_fitness=(None if fitness_value is None else float(fitness_value)),
        icp_rmse=(None if rmse_value is None else float(rmse_value)),
        icp_status=result.quality_status,
    )


def _set_icp_provenance(
    catalog: Any,
    input_data: IcpRuntimeInput,
    result: IcpRuntimeResult,
) -> None:
    """ICP A 편입과 같은 Undo snapshot 안에 provenance를 기록합니다."""

    setter = getattr(catalog, "set_alignment_provenance", None)
    if not callable(setter):
        raise IcpRuntimeError("ICP provenance를 기록할 Data Layers 기능이 없습니다.")
    setter(result.moving_model_id, _icp_provenance_record(input_data, result))


def approve_icp_result(runtime: Any) -> IcpAlignmentCommitSnapshot:
    """P alignment·A 그룹 편입·남은 마커 정리를 단일 Undo 작업으로 확정합니다."""

    state = _icp_state(runtime)
    if not state.has_preview or state.result is None or state.input_data is None:
        raise IcpRuntimeError("승인할 ICP 결과 미리보기가 없습니다.")
    result = state.result
    input_data = state.input_data
    if not _current_input_matches(runtime, input_data):
        state.clear(error_message="ICP 입력이 변경되어 결과를 승인할 수 없습니다. 다시 실행하십시오.")
        raise IcpRuntimeStaleError("ICP 입력이 변경되어 결과를 승인할 수 없습니다. 다시 실행하십시오.")
    repository = _repository_for_runtime(runtime)
    marker_state = getattr(runtime, "marker_alignment", None)
    marker_selection = getattr(runtime, "marker_selection", None)
    catalog = getattr(runtime, "model_catalog", None)
    journal = getattr(runtime, "undo_journal", None)
    if not isinstance(marker_state, MarkerAlignmentPreviewState) or not isinstance(journal, UndoJournal):
        raise IcpRuntimeError("ICP 승인에 필요한 Runtime 상태가 없습니다.")
    if not all(
        callable(getattr(marker_selection, method_name, None))
        for method_name in ("snapshot", "restore_snapshot", "clear")
    ):
        raise IcpRuntimeError("ICP 승인에 필요한 마커 선택 상태가 없습니다.")
    if catalog is None or not hasattr(catalog, "snapshot"):
        raise IcpRuntimeError("ICP 승인에 필요한 Data Layers 상태가 없습니다.")
    current_alignment = repository.alignment_transform_for_model_id(result.moving_model_id)
    expected_base = Sim3Transform(matrix_float64=input_data.seed.base_committed_alignment_float64)
    if current_alignment.fingerprint != expected_base.fingerprint:
        state.clear(error_message="P 확정 정렬이 변경되어 ICP 결과를 승인할 수 없습니다.")
        raise IcpRuntimeStaleError("P 확정 정렬이 변경되어 ICP 결과를 승인할 수 없습니다.")
    before = IcpCommitStateSnapshot(
        moving_model_id=result.moving_model_id,
        alignment=current_alignment,
        catalog_snapshot=catalog.snapshot(),
        marker_selection=marker_selection.snapshot(),
        marker_preview=_marker_preview_snapshot(marker_state),
    )
    try:
        if not repository.compare_and_set_alignment_transform(
            result.moving_model_id,
            expected_fingerprint=current_alignment.fingerprint,
            transform=result.final_alignment,
        ):
            raise IcpRuntimeStaleError("P 확정 정렬이 변경되어 ICP 결과를 승인할 수 없습니다.")
        _promote_catalog_for_icp(runtime, result.moving_model_id)
        _set_icp_provenance(catalog, input_data, result)
        marker_selection.clear()
        after = IcpCommitStateSnapshot(
            moving_model_id=result.moving_model_id,
            alignment=result.final_alignment,
            catalog_snapshot=catalog.snapshot(),
            marker_selection=marker_selection.snapshot(),
            marker_preview=IcpMarkerPreviewSnapshot(
                phase="IDLE",
                result=None,
                last_error="",
                started_at=None,
                p_view_snapshot=None,
            ),
        )
        commit = IcpAlignmentCommitSnapshot(
            before=before,
            after=after,
            input_fingerprint=result.input_fingerprint,
            result_fingerprint=_result_fingerprint(result),
        )
        if not journal.record_committed(
            before,
            after,
            "ICP 정렬 승인",
            _ICP_COMMAND_COMMIT,
        ):
            raise IcpRuntimeError("ICP 승인 Undo 이력을 기록하지 못했습니다.")
    except (IcpRuntimeError, IcpRuntimeStaleError, ModelRepositoryError, ValueError):
        repository.set_alignment_transform(result.moving_model_id, before.alignment)
        catalog.restore_snapshot(before.catalog_snapshot)
        marker_selection.restore_snapshot(before.marker_selection)
        _restore_marker_preview_snapshot(marker_state, before.marker_preview)
        raise
    marker_state.clear()
    state.phase = ICP_ALIGNMENT_PHASE_COMMITTED
    state.clear()
    from ..project_persistence.runtime import notify_project_state_changed

    notify_project_state_changed("ICP 정렬 승인", runtime=runtime)
    _LOGGER.info(
        "event=icp_result_committed moving_model_id=%s input_fingerprint=%s",
        result.moving_model_id,
        result.input_fingerprint,
    )
    return commit


def restore_icp_commit_snapshot(runtime: Any, snapshot: IcpCommitStateSnapshot) -> None:
    """Undo/Redo가 ICP alignment·카탈로그·마커 쌍·preview를 함께 복원합니다."""

    if not isinstance(snapshot, IcpCommitStateSnapshot):
        raise TypeError("ICP Undo snapshot 형식이 올바르지 않습니다.")
    repository = _repository_for_runtime(runtime)
    marker_state = getattr(runtime, "marker_alignment", None)
    marker_selection = getattr(runtime, "marker_selection", None)
    catalog = getattr(runtime, "model_catalog", None)
    if (
        not isinstance(marker_state, MarkerAlignmentPreviewState)
        or catalog is None
        or not all(
            callable(getattr(marker_selection, method_name, None))
            for method_name in ("snapshot", "restore_snapshot")
        )
    ):
        raise IcpRuntimeError("ICP Undo를 복원할 Runtime 상태가 없습니다.")
    current_alignment = repository.alignment_transform_for_model_id(snapshot.moving_model_id)
    current_catalog_snapshot = catalog.snapshot()
    current_marker_selection_snapshot = marker_selection.snapshot()
    current_marker_snapshot = _marker_preview_snapshot(marker_state)
    try:
        repository.set_alignment_transform(snapshot.moving_model_id, snapshot.alignment)
        catalog.restore_snapshot(snapshot.catalog_snapshot)
        marker_selection.restore_snapshot(snapshot.marker_selection)
        _restore_marker_preview_snapshot(marker_state, snapshot.marker_preview)
    except Exception:
        try:
            repository.set_alignment_transform(snapshot.moving_model_id, current_alignment)
            catalog.restore_snapshot(current_catalog_snapshot)
            marker_selection.restore_snapshot(current_marker_selection_snapshot)
            _restore_marker_preview_snapshot(marker_state, current_marker_snapshot)
        except Exception:
            _LOGGER.exception("event=icp_commit_restore_rollback_failed")
        raise
    _icp_state(runtime).clear()
    from ..project_persistence.runtime import notify_project_state_changed

    notify_project_state_changed("ICP 정렬 승인 Undo/Redo", runtime=runtime)


def invalidate_icp(runtime: Any, reason: str) -> None:
    """load·unregister·입력 변경 시 worker를 끝내고 ICP 결과만 안전하게 무효화합니다."""

    state = _icp_state(runtime)
    message = str(reason).strip() or "ICP 작업을 무효화했습니다."
    handle = state.job_handle
    try:
        if handle is not None and handle.is_running:
            request_job_cancel(handle)
            if wait_for_job_worker(handle, timeout_seconds=ICP_CANCEL_GRACE_SECONDS) is None:
                terminate_job_worker(handle)
                if wait_for_job_worker(handle, timeout_seconds=ICP_CANCEL_GRACE_SECONDS) is None:
                    kill_job_worker(handle)
                    if wait_for_job_worker(handle, timeout_seconds=ICP_CANCEL_GRACE_SECONDS) is None:
                        state.last_error = "ICP worker 종료를 확인하지 못해 작업 상태를 보존했습니다."
                        raise IcpRuntimeError(state.last_error)
        if handle is not None:
            if handle.is_running:
                state.last_error = "ICP worker가 아직 실행 중이어서 작업 상태를 보존했습니다."
                raise IcpRuntimeError(state.last_error)
            cleanup_job_directory(handle, keep_failure_diagnostics=False)
        _cleanup_retained_diagnostics(state)
    except IcpBackendError as error:
        state.last_error = f"ICP 작업 정리에 실패했습니다: {error}"
        raise IcpRuntimeError(state.last_error) from error
    state.clear(error_message=message)
    _LOGGER.info("event=icp_runtime_invalidated reason=%s", message)


def effective_icp_alignment_for_model(runtime: Any, model_id: str) -> Sim3Transform | None:
    """ICP 결과 preview 또는 마지막 안정 작업 transform이 현재 P에만 적용되는지 반환합니다."""

    state = _icp_state(runtime)
    normalized_model_id = str(model_id).strip()
    if not normalized_model_id:
        return None
    if state.has_preview and state.result is not None and state.result.moving_model_id == normalized_model_id:
        return state.result.final_alignment
    if (
        state.phase in {ICP_ALIGNMENT_PHASE_RUNNING, ICP_ALIGNMENT_PHASE_CANCEL_PENDING}
        and state.input_data is not None
        and state.input_data.seed.moving_model_id == normalized_model_id
        and state.working_alignment is not None
    ):
        return state.working_alignment
    return None


def icp_commit_command_kind() -> str:
    """세션 Undo 라우터가 비교할 ICP 승인 명령 종류를 반환합니다."""

    return _ICP_COMMAND_COMMIT


__all__ = (
    "IcpAlignmentCommitSnapshot",
    "IcpAlignmentRuntimeState",
    "IcpCommitStateSnapshot",
    "IcpProgressState",
    "IcpRuntimeError",
    "IcpRuntimeInput",
    "IcpRuntimeResult",
    "IcpRuntimeStaleError",
    "approve_icp_result",
    "begin_icp_job",
    "create_icp_input_snapshot",
    "discard_icp_preview",
    "effective_icp_alignment_for_model",
    "finish_icp_job",
    "icp_commit_command_kind",
    "invalidate_icp",
    "is_icp_result_approvable",
    "poll_icp_job",
    "request_icp_cancel",
    "restore_icp_commit_snapshot",
)
