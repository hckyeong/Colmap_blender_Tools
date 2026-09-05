"""Data Layers 선택 subset의 비파괴 Project World G/R/S 변환을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, isfinite, sin
from typing import Any

import numpy as np

from ..constants import (
    LAYER_TRANSFORM_AXIS_NONE,
    LAYER_TRANSFORM_COMMAND_COMMIT,
    LAYER_TRANSFORM_PHASE_IDLE,
    LAYER_TRANSFORM_PHASE_ROTATE,
    LAYER_TRANSFORM_PHASE_SCALE,
    LAYER_TRANSFORM_PHASE_TRANSLATE,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from .types import (
    AlignmentProvenanceRecord,
    Sim3Transform,
    compose_sim3_transforms,
    identity_sim3_transform,
)


_VALID_ROLES = frozenset({ROLE_REFERENCE, ROLE_MOVING})
_VALID_PHASES = frozenset(
    {
        LAYER_TRANSFORM_PHASE_TRANSLATE,
        LAYER_TRANSFORM_PHASE_ROTATE,
        LAYER_TRANSFORM_PHASE_SCALE,
    }
)
_VALID_AXES = frozenset({LAYER_TRANSFORM_AXIS_NONE, "X", "Y", "Z"})
_EPSILON = float(np.finfo(np.float64).eps)


class LayerTransformError(RuntimeError):
    """레이어 변환의 선택·수학·원자성 계약을 위반했을 때 발생합니다."""


def _vector3(value: Any, field_name: str) -> np.ndarray:
    """유한한 Project World XYZ 입력을 독립 float64 벡터로 검증합니다."""

    try:
        vector = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise LayerTransformError(f"{field_name} 좌표 형식이 올바르지 않습니다.") from error
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise LayerTransformError(f"{field_name} 좌표는 유한한 XYZ여야 합니다.")
    return np.ascontiguousarray(vector, dtype=np.float64)


def _pointer(value: Any) -> int:
    """Blender RNA 객체 또는 정수 포인터를 안전하게 작은 식별자로 정규화합니다."""

    if value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(value.as_pointer()))
    except (AttributeError, ReferenceError, TypeError, ValueError):
        return 0


def _repository(runtime: Any) -> Any:
    """실제 확정 alignment 저장소를 가진 Repository를 검증해 반환합니다."""

    repository = getattr(runtime, "model_repository", None)
    if repository is None or not isinstance(getattr(repository, "models", None), dict):
        raise LayerTransformError("레이어 변환에 사용할 COLMAP 모델 저장소가 없습니다.")
    return repository


def _catalog(runtime: Any) -> Any:
    """선택과 provenance를 소유한 Data Layers 카탈로그를 검증해 반환합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    if catalog is None or not isinstance(getattr(catalog, "models", None), dict):
        raise LayerTransformError("레이어 변환에 사용할 Data Layers 카탈로그가 없습니다.")
    return catalog


def _alignment(repository: Any, model_id: str) -> Sim3Transform:
    """등록 모델의 확정 Sim(3)을 누락·형식 오류 없이 조회합니다."""

    try:
        alignment = repository.alignment_transform_for_model_id(model_id)
    except (AttributeError, KeyError, RuntimeError, ValueError) as error:
        raise LayerTransformError(f"레이어 변환 기준 alignment를 찾을 수 없습니다: {model_id}") from error
    if not isinstance(alignment, Sim3Transform):
        raise LayerTransformError(f"레이어 변환 기준 alignment 형식이 올바르지 않습니다: {model_id}")
    return alignment


def _display_matrix(repository: Any, model_id: str) -> np.ndarray:
    """모델 원점 계산에 필요한 불변 Display Axis 행렬을 검증해 반환합니다."""

    try:
        display_transform = repository.display_transform_for_model_id(model_id)
        matrix = np.asarray(display_transform.matrix_float64, dtype=np.float64)
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
        raise LayerTransformError(f"레이어 변환 표시 축을 찾을 수 없습니다: {model_id}") from error
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise LayerTransformError(f"레이어 변환 표시 축 행렬이 올바르지 않습니다: {model_id}")
    return np.ascontiguousarray(matrix, dtype=np.float64)


def selected_model_ids_for_role(catalog: Any, role: str) -> tuple[str, ...]:
    """현재 Data Layers 다중 선택에서 지정 뷰포트 역할의 순서 보존 대상만 반환합니다."""

    if role == ROLE_REFERENCE:
        selected = getattr(catalog, "selected_reference_model_ids", ())
    elif role == ROLE_MOVING:
        selected = getattr(catalog, "selected_moving_model_ids", ())
    else:
        return ()
    return tuple(str(model_id).strip() for model_id in selected if str(model_id).strip())


def project_world_model_origin(repository: Any, model_id: str) -> np.ndarray:
    """`Alignment × Display Axis × origin`으로 모델 원점의 Project World 좌표를 계산합니다."""

    alignment = _alignment(repository, model_id)
    homogeneous = alignment.matrix_float64 @ _display_matrix(repository, model_id) @ np.asarray(
        (0.0, 0.0, 0.0, 1.0), dtype=np.float64
    )
    weight = float(homogeneous[3])
    if not np.isfinite(homogeneous).all() or abs(weight) <= _EPSILON:
        raise LayerTransformError(f"모델 Project World 원점을 계산할 수 없습니다: {model_id}")
    return np.ascontiguousarray(homogeneous[:3] / weight, dtype=np.float64)


def project_world_source_position(repository: Any, model_id: str, source_position: Any) -> np.ndarray:
    """한 원본 좌표를 `Alignment × Display Axis`로 Project World에 변환합니다."""

    source = _vector3(source_position, "피벗 원본 점")
    homogeneous = _alignment(repository, model_id).matrix_float64 @ _display_matrix(
        repository,
        model_id,
    ) @ np.asarray((*source, 1.0), dtype=np.float64)
    weight = float(homogeneous[3])
    if not np.isfinite(homogeneous).all() or abs(weight) <= _EPSILON:
        raise LayerTransformError(f"피벗의 Project World 좌표를 계산할 수 없습니다: {model_id}")
    return np.ascontiguousarray(homogeneous[:3] / weight, dtype=np.float64)


def common_project_world_pivot(repository: Any, model_ids: tuple[str, ...]) -> np.ndarray:
    """선택 모델의 Project World 원점 산술 평균을 공통 G/R/S pivot으로 반환합니다."""

    if not model_ids:
        raise LayerTransformError("변환할 데이터 레이어가 없습니다.")
    origins = np.asarray(
        [project_world_model_origin(repository, model_id) for model_id in model_ids],
        dtype=np.float64,
    )
    pivot = np.mean(origins, axis=0, dtype=np.float64)
    if pivot.shape != (3,) or not np.isfinite(pivot).all():
        raise LayerTransformError("데이터 레이어 공통 pivot을 계산할 수 없습니다.")
    return np.ascontiguousarray(pivot, dtype=np.float64)


def _pivoted_delta(linear: np.ndarray, pivot: Any) -> Sim3Transform:
    """Project World pivot을 고정하는 양의 균일 Sim(3) delta를 생성합니다."""

    pivot_vector = _vector3(pivot, "변환 pivot")
    matrix = np.asarray(linear, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise LayerTransformError("레이어 변환 선형 행렬이 올바르지 않습니다.")
    result = np.identity(4, dtype=np.float64)
    result[:3, :3] = matrix
    result[:3, 3] = pivot_vector - matrix @ pivot_vector
    try:
        return Sim3Transform(matrix_float64=result)
    except (TypeError, ValueError, np.linalg.LinAlgError) as error:
        raise LayerTransformError("레이어 변환 delta가 유효한 Sim(3)이 아닙니다.") from error


def create_translation_delta(translation: Any) -> Sim3Transform:
    """Project World 평행이동만 포함한 left-composition delta를 생성합니다."""

    vector = _vector3(translation, "이동")
    matrix = np.identity(4, dtype=np.float64)
    matrix[:3, 3] = vector
    try:
        return Sim3Transform(matrix_float64=matrix)
    except (TypeError, ValueError, np.linalg.LinAlgError) as error:
        raise LayerTransformError("이동 delta를 생성할 수 없습니다.") from error


def create_rotation_delta(angle_radians: float, axis: Any, pivot: Any) -> Sim3Transform:
    """지정 축·각도의 Project World pivot 회전 delta를 생성합니다."""

    angle = float(angle_radians)
    axis_vector = _vector3(axis, "회전 축")
    axis_length = float(np.linalg.norm(axis_vector))
    if not isfinite(angle) or axis_length <= _EPSILON:
        raise LayerTransformError("회전 각도 또는 축이 유효하지 않습니다.")
    x, y, z = axis_vector / axis_length
    sine = sin(angle)
    cosine = cos(angle)
    complement = 1.0 - cosine
    rotation = np.asarray(
        (
            (cosine + x * x * complement, x * y * complement - z * sine, x * z * complement + y * sine),
            (y * x * complement + z * sine, cosine + y * y * complement, y * z * complement - x * sine),
            (z * x * complement - y * sine, z * y * complement + x * sine, cosine + z * z * complement),
        ),
        dtype=np.float64,
    )
    return _pivoted_delta(rotation, pivot)


def create_uniform_scale_delta(scale: float, pivot: Any) -> Sim3Transform:
    """음수·0·비균일 값을 허용하지 않는 Project World 균일 스케일 delta를 생성합니다."""

    normalized_scale = float(scale)
    if not isfinite(normalized_scale) or normalized_scale <= 0.0:
        raise LayerTransformError("레이어 스케일은 유한한 양수의 균일 값이어야 합니다.")
    return _pivoted_delta(np.identity(3, dtype=np.float64) * normalized_scale, pivot)


@dataclass(frozen=True, slots=True)
class LayerTransformModelSnapshot:
    """한 레이어의 확정 alignment와 provenance를 원자 복원용으로 고정합니다."""

    model_id: str
    alignment: Sim3Transform
    alignment_provenance: AlignmentProvenanceRecord | None

    def __post_init__(self) -> None:
        """스냅샷 식별자와 transform·provenance 소유권을 검증합니다."""

        model_id = str(self.model_id).strip()
        if not model_id or not isinstance(self.alignment, Sim3Transform):
            raise LayerTransformError("레이어 변환 스냅샷 형식이 올바르지 않습니다.")
        provenance = self.alignment_provenance
        if provenance is not None:
            if not isinstance(provenance, AlignmentProvenanceRecord) or provenance.model_id != model_id:
                raise LayerTransformError("레이어 변환 provenance 스냅샷이 올바르지 않습니다.")
        object.__setattr__(self, "model_id", model_id)


@dataclass(frozen=True, slots=True)
class LayerTransformCommitSnapshot:
    """여러 레이어를 하나의 Undo/Redo 경계로 복원할 immutable snapshot입니다."""

    models: tuple[LayerTransformModelSnapshot, ...]

    def __post_init__(self) -> None:
        """복원 대상의 공백·중복을 거부해 부분 적용을 방지합니다."""

        models = tuple(self.models)
        if not models or any(not isinstance(model, LayerTransformModelSnapshot) for model in models):
            raise LayerTransformError("레이어 변환 확정 스냅샷이 비어 있거나 형식이 올바르지 않습니다.")
        if len({model.model_id for model in models}) != len(models):
            raise LayerTransformError("레이어 변환 확정 스냅샷에 중복 모델이 있습니다.")
        object.__setattr__(self, "models", models)


@dataclass
class LayerTransformRuntimeState:
    """점 배열을 복사하지 않고 G/R/S 미리보기와 사용자 피벗 앵커를 보관합니다."""

    phase: str = LAYER_TRANSFORM_PHASE_IDLE
    generation: int = 0
    role: str = ""
    model_ids: tuple[str, ...] = ()
    baseline_alignments: dict[str, Sim3Transform] = field(default_factory=dict)
    baseline_fingerprints: dict[str, str] = field(default_factory=dict)
    baseline_provenance: dict[str, AlignmentProvenanceRecord | None] = field(default_factory=dict)
    pivot_float64: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    preview_delta: Sim3Transform = field(default_factory=identity_sim3_transform)
    constraint_axis: str = LAYER_TRANSFORM_AXIS_NONE
    window_pointer: int = 0
    area_pointer: int = 0
    region_pointer: int = 0
    session_generation: int = 0
    message: str = ""
    custom_pivot_role: str = ""
    custom_pivot_model_ids: tuple[str, ...] = ()
    custom_pivot_source_model_id: str = ""
    custom_pivot_source_position_float64: np.ndarray = field(
        default_factory=lambda: np.zeros(3, dtype=np.float64)
    )
    pivot_click_time: float = 0.0
    pivot_click_window_pointer: int = 0
    pivot_click_area_pointer: int = 0
    pivot_click_mouse: tuple[int, int] = (0, 0)

    @property
    def is_active(self) -> bool:
        """현재 Repository를 건드리지 않는 G/R/S 미리보기가 활성인지 반환합니다."""

        return self.phase in _VALID_PHASES and bool(self.model_ids)

    def clear(self, *, message: str = "", preserve_custom_pivot: bool = False) -> None:
        """미리보기를 폐기하고 요청된 경우에만 더블클릭 피벗 앵커를 유지합니다."""

        self.generation += 1
        self.phase = LAYER_TRANSFORM_PHASE_IDLE
        self.role = ""
        self.model_ids = ()
        self.baseline_alignments.clear()
        self.baseline_fingerprints.clear()
        self.baseline_provenance.clear()
        self.pivot_float64 = np.zeros(3, dtype=np.float64)
        self.preview_delta = identity_sim3_transform()
        self.constraint_axis = LAYER_TRANSFORM_AXIS_NONE
        self.window_pointer = 0
        self.area_pointer = 0
        self.region_pointer = 0
        self.session_generation = 0
        self.message = str(message).strip()
        self.clear_pivot_click()
        if not preserve_custom_pivot:
            self.clear_custom_pivot()

    def clear_custom_pivot(self) -> None:
        """세션 수명에 묶인 더블클릭 원본 점 앵커를 초기화합니다."""

        self.custom_pivot_role = ""
        self.custom_pivot_model_ids = ()
        self.custom_pivot_source_model_id = ""
        self.custom_pivot_source_position_float64 = np.zeros(3, dtype=np.float64)

    def clear_pivot_click(self) -> None:
        """뷰포트 더블클릭 판정에 사용한 첫 번째 클릭 정보를 비웁니다."""

        self.pivot_click_time = 0.0
        self.pivot_click_window_pointer = 0
        self.pivot_click_area_pointer = 0
        self.pivot_click_mouse = (0, 0)


def _runtime_state(runtime: Any) -> LayerTransformRuntimeState:
    """Runtime의 작은 레이어 변환 상태를 누락 없이 조회합니다."""

    state = getattr(runtime, "layer_transform", None)
    if not isinstance(state, LayerTransformRuntimeState):
        raise LayerTransformError("레이어 변환 런타임 상태가 없습니다.")
    return state


def set_layer_transform_custom_pivot(
    runtime: Any,
    *,
    role: str,
    model_ids: tuple[str, ...],
    source_model_id: str,
    source_position: Any,
) -> np.ndarray:
    """선택 subset에서 피킹한 원본 점을 추적 가능한 사용자 피벗으로 설정합니다."""

    state = _runtime_state(runtime)
    if state.is_active:
        raise LayerTransformError("진행 중인 레이어 변환에서는 피벗을 바꿀 수 없습니다.")
    normalized_role = str(role).strip()
    normalized_ids = tuple(str(model_id).strip() for model_id in model_ids if str(model_id).strip())
    normalized_source_id = str(source_model_id).strip()
    if normalized_role not in _VALID_ROLES or not normalized_ids:
        raise LayerTransformError("사용자 피벗의 역할 또는 선택 레이어가 올바르지 않습니다.")
    if len(set(normalized_ids)) != len(normalized_ids) or normalized_source_id not in normalized_ids:
        raise LayerTransformError("사용자 피벗의 선택 레이어 구성이 올바르지 않습니다.")
    repository = _repository(runtime)
    catalog = _catalog(runtime)
    if any(model_id not in repository.models or model_id not in catalog.models for model_id in normalized_ids):
        raise LayerTransformError("사용자 피벗 대상 레이어를 찾을 수 없습니다.")
    normalized_source_position = _vector3(source_position, "피벗 원본 점")
    pivot = project_world_source_position(repository, normalized_source_id, normalized_source_position)
    state.custom_pivot_role = normalized_role
    state.custom_pivot_model_ids = normalized_ids
    state.custom_pivot_source_model_id = normalized_source_id
    state.custom_pivot_source_position_float64 = normalized_source_position
    return pivot


def layer_transform_pivot_for_selection(
    runtime: Any,
    role: str,
    model_ids: tuple[str, ...],
) -> np.ndarray:
    """현재 선택과 일치하는 사용자 피벗 또는 기본 원점 평균 피벗을 반환합니다."""

    state = _runtime_state(runtime)
    normalized_role = str(role).strip()
    normalized_ids = tuple(str(model_id).strip() for model_id in model_ids if str(model_id).strip())
    if (
        state.custom_pivot_role == normalized_role
        and state.custom_pivot_model_ids == normalized_ids
        and state.custom_pivot_source_model_id in normalized_ids
    ):
        return project_world_source_position(
            _repository(runtime),
            state.custom_pivot_source_model_id,
            state.custom_pivot_source_position_float64,
        )
    return common_project_world_pivot(_repository(runtime), normalized_ids)


def begin_layer_transform(
    runtime: Any,
    *,
    role: str,
    model_ids: tuple[str, ...] | None = None,
    phase: str = LAYER_TRANSFORM_PHASE_TRANSLATE,
    window: Any = None,
    area: Any = None,
    region: Any = None,
) -> LayerTransformRuntimeState:
    """선택 역할 subset의 확정 기준값·pivot·입력 포인터를 한 번에 동결합니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(runtime)
    if axis_reason is not None:
        raise LayerTransformError(axis_reason)

    normalized_role = str(role).strip()
    normalized_phase = str(phase).strip().upper()
    if normalized_role not in _VALID_ROLES or normalized_phase not in _VALID_PHASES:
        raise LayerTransformError("레이어 변환 역할 또는 모드가 올바르지 않습니다.")
    state = _runtime_state(runtime)
    if state.is_active:
        raise LayerTransformError("다른 레이어 변환이 이미 진행 중입니다.")
    repository = _repository(runtime)
    catalog = _catalog(runtime)
    requested_ids = selected_model_ids_for_role(catalog, normalized_role) if model_ids is None else tuple(model_ids)
    ordered_ids: list[str] = []
    seen: set[str] = set()
    catalog_order = tuple(getattr(catalog, "model_order", ()))
    requested_set = {str(model_id).strip() for model_id in requested_ids if str(model_id).strip()}
    for model_id in catalog_order:
        normalized_id = str(model_id).strip()
        if normalized_id in requested_set and normalized_id not in seen:
            ordered_ids.append(normalized_id)
            seen.add(normalized_id)
    for model_id in requested_ids:
        normalized_id = str(model_id).strip()
        if normalized_id and normalized_id not in seen:
            ordered_ids.append(normalized_id)
            seen.add(normalized_id)
    if not ordered_ids:
        raise LayerTransformError("현재 역할 뷰포트에서 변환할 선택 레이어가 없습니다.")
    for model_id in ordered_ids:
        if model_id not in repository.models or model_id not in catalog.models:
            raise LayerTransformError(f"레이어 변환 대상 모델을 찾을 수 없습니다: {model_id}")
    baseline_alignments = {model_id: _alignment(repository, model_id) for model_id in ordered_ids}
    state.generation += 1
    state.phase = normalized_phase
    state.role = normalized_role
    state.model_ids = tuple(ordered_ids)
    state.baseline_alignments = baseline_alignments
    state.baseline_fingerprints = {
        model_id: alignment.fingerprint for model_id, alignment in baseline_alignments.items()
    }
    state.baseline_provenance = {
        model_id: getattr(catalog.models[model_id], "alignment_provenance", None)
        for model_id in ordered_ids
    }
    state.pivot_float64 = layer_transform_pivot_for_selection(runtime, normalized_role, state.model_ids)
    state.preview_delta = identity_sim3_transform()
    state.constraint_axis = LAYER_TRANSFORM_AXIS_NONE
    state.window_pointer = _pointer(window)
    state.area_pointer = _pointer(area)
    state.region_pointer = _pointer(region)
    state.session_generation = int(getattr(getattr(runtime, "session", None), "generation", 0))
    state.message = ""
    return state


def set_layer_transform_preview_delta(runtime: Any, delta: Sim3Transform) -> None:
    """미리보기 delta만 교체해 렌더·피킹이 즉시 같은 유효 alignment를 보게 합니다."""

    state = _runtime_state(runtime)
    if not state.is_active:
        raise LayerTransformError("진행 중인 레이어 변환이 없습니다.")
    if not isinstance(delta, Sim3Transform):
        raise LayerTransformError("레이어 변환 미리보기는 유효한 Sim(3)이어야 합니다.")
    state.preview_delta = delta


def set_layer_transform_constraint(runtime: Any, axis: str) -> str:
    """X/Y/Z 같은 축을 한 번 더 누르면 해제하는 모달 제약 상태를 갱신합니다."""

    state = _runtime_state(runtime)
    normalized_axis = str(axis).strip().upper()
    if normalized_axis not in _VALID_AXES:
        raise LayerTransformError("레이어 변환 축 제약이 올바르지 않습니다.")
    state.constraint_axis = (
        LAYER_TRANSFORM_AXIS_NONE
        if normalized_axis != LAYER_TRANSFORM_AXIS_NONE and state.constraint_axis == normalized_axis
        else normalized_axis
    )
    return state.constraint_axis


def effective_layer_transform_alignment(runtime: Any, model_id: str) -> Sim3Transform | None:
    """활성 subset 모델에만 `preview delta × baseline` 유효 alignment를 반환합니다."""

    state = _runtime_state(runtime)
    normalized_id = str(model_id).strip()
    if not state.is_active or normalized_id not in state.model_ids:
        return None
    baseline = state.baseline_alignments.get(normalized_id)
    if not isinstance(baseline, Sim3Transform):
        raise LayerTransformError("레이어 변환 기준 alignment가 손상되었습니다.")
    try:
        return compose_sim3_transforms(state.preview_delta, baseline)
    except (TypeError, ValueError, np.linalg.LinAlgError) as error:
        raise LayerTransformError("레이어 변환 미리보기 Sim(3)을 계산할 수 없습니다.") from error


def cancel_layer_transform(runtime: Any, *, message: str = "") -> bool:
    """확정 저장소를 전혀 건드리지 않고 활성 미리보기만 취소합니다."""

    state = _runtime_state(runtime)
    if not state.is_active:
        return False
    state.clear(message=message, preserve_custom_pivot=True)
    return True


def _snapshot(runtime: Any, model_ids: tuple[str, ...]) -> LayerTransformCommitSnapshot:
    """현재 확정 alignment·provenance를 복원 가능한 불변 묶음으로 캡처합니다."""

    repository = _repository(runtime)
    catalog = _catalog(runtime)
    snapshots = []
    for model_id in model_ids:
        record = catalog.models.get(model_id)
        if record is None or model_id not in repository.models:
            raise LayerTransformError(f"레이어 변환 스냅샷 대상이 사라졌습니다: {model_id}")
        snapshots.append(
            LayerTransformModelSnapshot(
                model_id=model_id,
                alignment=_alignment(repository, model_id),
                alignment_provenance=getattr(record, "alignment_provenance", None),
            )
        )
    return LayerTransformCommitSnapshot(models=tuple(snapshots))


def _set_provenance(catalog: Any, model_id: str, provenance: AlignmentProvenanceRecord | None) -> None:
    """None을 포함한 provenance를 카탈로그 한 모델에 명시적으로 복원합니다."""

    setter = getattr(catalog, "set_alignment_provenance", None)
    if not callable(setter):
        raise LayerTransformError("레이어 변환 provenance를 기록할 카탈로그 함수가 없습니다.")
    setter(model_id, provenance)


def _restore_snapshot_atomic(runtime: Any, snapshot: LayerTransformCommitSnapshot) -> None:
    """모든 대상 검증 뒤 alignment와 provenance를 전부 복원하고 실패 시 되돌립니다."""

    if not isinstance(snapshot, LayerTransformCommitSnapshot):
        raise LayerTransformError("레이어 변환 복원 스냅샷 형식이 올바르지 않습니다.")
    repository = _repository(runtime)
    catalog = _catalog(runtime)
    for model in snapshot.models:
        if model.model_id not in repository.models or model.model_id not in catalog.models:
            raise LayerTransformError(f"레이어 변환 복원 대상 모델을 찾을 수 없습니다: {model.model_id}")
    rollback = _snapshot(runtime, tuple(model.model_id for model in snapshot.models))
    try:
        for model in snapshot.models:
            repository.set_alignment_transform(model.model_id, model.alignment)
        for model in snapshot.models:
            _set_provenance(catalog, model.model_id, model.alignment_provenance)
    except Exception as error:
        try:
            for model in rollback.models:
                repository.set_alignment_transform(model.model_id, model.alignment)
            for model in rollback.models:
                _set_provenance(catalog, model.model_id, model.alignment_provenance)
        except Exception as rollback_error:
            raise LayerTransformError("레이어 변환 복원과 롤백을 모두 완료하지 못했습니다.") from rollback_error
        raise LayerTransformError("레이어 변환 복원에 실패해 이전 확정 상태로 되돌렸습니다.") from error


def commit_layer_transform(
    runtime: Any,
    *,
    action: str = "데이터 레이어 변환",
) -> tuple[LayerTransformCommitSnapshot, LayerTransformCommitSnapshot] | None:
    """모든 후보 검증·기록·Undo 등록을 성공할 때만 한 번의 확정 경계로 적용합니다."""

    state = _runtime_state(runtime)
    if not state.is_active:
        raise LayerTransformError("확정할 레이어 변환이 없습니다.")
    current_session_generation = int(getattr(getattr(runtime, "session", None), "generation", 0))
    if state.session_generation != current_session_generation:
        raise LayerTransformError("세션이 변경되어 레이어 변환을 확정할 수 없습니다.")
    repository = _repository(runtime)
    catalog = _catalog(runtime)
    for model_id in state.model_ids:
        if model_id not in repository.models or model_id not in catalog.models:
            raise LayerTransformError(f"레이어 변환 대상 모델이 사라졌습니다: {model_id}")
        if _alignment(repository, model_id).fingerprint != state.baseline_fingerprints.get(model_id):
            raise LayerTransformError("확정 기준 alignment가 변경되어 레이어 변환을 확정할 수 없습니다.")
    candidates = {
        model_id: compose_sim3_transforms(state.preview_delta, state.baseline_alignments[model_id])
        for model_id in state.model_ids
    }
    if all(
        candidates[model_id].fingerprint == state.baseline_fingerprints[model_id]
        for model_id in state.model_ids
    ):
        state.clear(preserve_custom_pivot=True)
        return None
    before = _snapshot(runtime, state.model_ids)
    manual_provenance = {
        model_id: AlignmentProvenanceRecord(
            model_id=model_id,
            method="MANUAL",
            result_fingerprint=candidates[model_id].fingerprint,
        )
        for model_id in state.model_ids
    }
    try:
        for model_id, candidate in candidates.items():
            repository.set_alignment_transform(model_id, candidate)
        for model_id, provenance in manual_provenance.items():
            _set_provenance(catalog, model_id, provenance)
        after = _snapshot(runtime, state.model_ids)
        journal = getattr(runtime, "undo_journal", None)
        if journal is None or not journal.record_committed(
            before,
            after,
            str(action).strip() or "데이터 레이어 변환",
            LAYER_TRANSFORM_COMMAND_COMMIT,
        ):
            raise LayerTransformError("레이어 변환 Undo 이력을 기록하지 못했습니다.")
    except Exception as error:
        _restore_snapshot_atomic(runtime, before)
        raise LayerTransformError("레이어 변환 확정에 실패해 모든 레이어를 이전 상태로 되돌렸습니다.") from error
    state.clear(preserve_custom_pivot=True)
    return before, after


def restore_layer_transform_commit_snapshot(runtime: Any, snapshot: LayerTransformCommitSnapshot) -> None:
    """세션 Undo/Redo가 지정 snapshot 전체를 원자적으로 복원하도록 처리합니다."""

    _restore_snapshot_atomic(runtime, snapshot)
    cancel_layer_transform(runtime, message="Undo/Redo로 레이어 변환 미리보기를 정리했습니다.")


__all__ = (
    "LayerTransformCommitSnapshot",
    "LayerTransformError",
    "LayerTransformModelSnapshot",
    "LayerTransformRuntimeState",
    "begin_layer_transform",
    "cancel_layer_transform",
    "commit_layer_transform",
    "common_project_world_pivot",
    "create_rotation_delta",
    "create_translation_delta",
    "create_uniform_scale_delta",
    "effective_layer_transform_alignment",
    "layer_transform_pivot_for_selection",
    "project_world_model_origin",
    "project_world_source_position",
    "restore_layer_transform_commit_snapshot",
    "selected_model_ids_for_role",
    "set_layer_transform_constraint",
    "set_layer_transform_custom_pivot",
    "set_layer_transform_preview_delta",
)
