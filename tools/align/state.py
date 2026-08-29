"""Blender 전역 선택과 분리된 COLMAP Align 세션 상태를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Any

from .alignment.layer_transform import LayerTransformRuntimeState
from .alignment.runtime import MarkerAlignmentPreviewState
from .alignment.icp_runtime import IcpAlignmentRuntimeState
from .alignment.types import AlignmentProvenanceRecord
from .constants import (
    DEMO_MODEL_1_ID,
    DEMO_MODEL_2_ID,
    DEMO_MODEL_3_ID,
    DEMO_MODEL_4_ID,
    DRAFT_MODE_CREATE,
    DRAFT_MODE_EDIT,
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_COMMAND_CANCEL,
    MARKER_COMMAND_CONFIRM,
    MARKER_COMMAND_NONE,
    MARKER_ALIGNMENT_POSITION_TOLERANCE,
    MERGE_EXPORT_PHASE_CANCEL_PENDING,
    MERGE_EXPORT_PHASE_COMPLETED,
    MERGE_EXPORT_PHASE_EXPORT_RUNNING,
    MERGE_EXPORT_PHASE_FAILED,
    MERGE_EXPORT_PHASE_IDLE,
    MERGE_EXPORT_PHASE_PREFLIGHT_READY,
    MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING,
    MERGE_EXPORT_PHASE_WAITING_FOR_FINALIZE,
    OBJ_EXPORT_PHASE_CANCEL_PENDING,
    OBJ_EXPORT_PHASE_FINALIZING,
    OBJ_EXPORT_PHASE_IDLE,
    OBJ_EXPORT_PHASE_RUNNING,
    MODEL_ACTIVATION_SOURCE_ALIGNMENT,
    MODEL_ACTIVATION_SOURCE_MANUAL,
    MODEL_STATUS_ALIGNING,
    MODEL_STATUS_PENDING,
    MODEL_STATUS_PREVIEW,
    MODEL_STATUS_REGISTERED,
    PHASE_CAPTURING,
    PHASE_IDLE,
    PROJECT_RESTORE_PHASE_IDLE,
    ROLE_MOVING,
    ROLE_REFERENCE,
    MARKER_POINT_KIND_COLMAP,
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KINDS,
    SESSION_ENTERING,
    SESSION_INACTIVE,
)
from .model_repository import ColmapModelRepository
from .undo import UndoJournal


_VALID_ROLES = frozenset({ROLE_REFERENCE, ROLE_MOVING})
_INACTIVE_MODEL_STATUSES = frozenset(
    {MODEL_STATUS_PENDING, MODEL_STATUS_ALIGNING, MODEL_STATUS_PREVIEW}
)
_VALID_MODEL_ACTIVATION_SOURCES = frozenset(
    {MODEL_ACTIVATION_SOURCE_MANUAL, MODEL_ACTIVATION_SOURCE_ALIGNMENT}
)


@dataclass(frozen=True)
class ModelCatalogRecord:
    """Data Layers가 참조하는 한 모델의 런타임 상태입니다."""

    model_id: str
    display_name: str
    load_order: int
    registration_status: str
    is_anchor: bool = False
    is_visible: bool = True
    alignment_provenance: AlignmentProvenanceRecord | None = None
    geometry_kind: str = GEOMETRY_KIND_COLMAP_POINT_CLOUD

    def __post_init__(self) -> None:
        """카탈로그 형상 종류를 두 지원 값으로 정규화합니다."""
        geometry_kind = str(self.geometry_kind).strip().upper()
        if geometry_kind not in {GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH}:
            raise ValueError("Data Layer geometry kind가 올바르지 않습니다.")
        object.__setattr__(self, "geometry_kind", geometry_kind)


@dataclass
class MergeExportRuntimeState:
    """대규모 결과를 복사하지 않고 구조 통합 worker의 작은 상태만 보관합니다."""

    phase: str = MERGE_EXPORT_PHASE_IDLE
    backend: Any = None
    job: Any = None
    snapshot: Any = None
    preflight: Any = None
    progress: Any = None
    result: Any = None
    last_issue: Any = None
    last_error: str = ""
    last_success_output_root: str = ""
    last_success_report_path: str = ""
    last_diagnostic_path: str = ""

    @property
    def is_running(self) -> bool:
        """입력 변경을 잠가야 하는 preflight·worker·최종 확정 단계를 반환합니다."""

        return self.job is not None or self.phase in {
            MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING,
            MERGE_EXPORT_PHASE_EXPORT_RUNNING,
            MERGE_EXPORT_PHASE_CANCEL_PENDING,
            MERGE_EXPORT_PHASE_WAITING_FOR_FINALIZE,
        }

    @property
    def is_preflight_ready(self) -> bool:
        """현재 snapshot에 대한 성공 preflight가 남아 있는지 반환합니다."""

        return self.phase == MERGE_EXPORT_PHASE_PREFLIGHT_READY and self.preflight is not None

    def clear(self, *, error_message: str = "") -> None:
        """worker 참조와 UI용 결과를 모두 비우고 유휴 상태로 되돌립니다."""

        self.phase = MERGE_EXPORT_PHASE_IDLE
        self.backend = None
        self.job = None
        self.snapshot = None
        self.preflight = None
        self.progress = None
        self.result = None
        self.last_issue = None
        self.last_error = str(error_message).strip()
        self.last_success_output_root = ""
        self.last_success_report_path = ""
        self.last_diagnostic_path = ""


@dataclass
class ObjExportRuntimeState:
    """Blender TIMER OBJ Export이 보유한 작은 진행·결과 상태를 관리합니다."""

    phase: str = OBJ_EXPORT_PHASE_IDLE
    job: Any = None
    snapshot: Any = None
    progress: Any = None
    result: Any = None
    last_error: str = ""
    last_success_output_root: str = ""
    last_success_report_path: str = ""
    last_vertex_count: int = 0
    last_face_count: int = 0

    @property
    def is_running(self) -> bool:
        """입력 변경을 잠가야 하는 OBJ 기록·취소 대기·최종 확정 단계를 반환합니다."""

        return self.job is not None or self.phase in {
            OBJ_EXPORT_PHASE_RUNNING,
            OBJ_EXPORT_PHASE_CANCEL_PENDING,
            OBJ_EXPORT_PHASE_FINALIZING,
        }

    def clear(self, *, error_message: str = "") -> None:
        """OBJ writer·timer 결과 참조를 비우고 지정한 오류만 UI에 남깁니다."""

        self.phase = OBJ_EXPORT_PHASE_IDLE
        self.job = None
        self.snapshot = None
        self.progress = None
        self.result = None
        self.last_error = str(error_message).strip()
        self.last_success_output_root = ""
        self.last_success_report_path = ""
        self.last_vertex_count = 0
        self.last_face_count = 0


@dataclass(frozen=True)
class ModelCatalogSnapshot:
    """그룹 전환 Undo가 복원할 카탈로그의 불변 상태를 보관합니다."""

    records: tuple[ModelCatalogRecord, ...]
    model_order: tuple[str, ...]
    selected_model_id: str | None
    selected_reference_model_id: str | None
    selected_moving_model_id: str | None
    selected_model_ids: tuple[str, ...] = ()
    selection_anchor_model_id: str | None = None


@dataclass
class ModelCatalogState:
    """Data Layers의 다중 선택·활성 행·A/P 역할별 파생 선택과 표시 상태를 관리합니다."""

    models: dict[str, ModelCatalogRecord] = field(default_factory=dict)
    model_order: list[str] = field(default_factory=list)
    selected_model_id: str | None = None
    selected_reference_model_id: str | None = None
    selected_moving_model_id: str | None = None
    selected_model_ids: tuple[str, ...] = ()
    selection_anchor_model_id: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        """초기 선택을 공통 집합으로 정규화해 A/P 파생값을 별도 입력 없이 계산합니다."""

        initial_ids = self.selected_model_ids
        if not initial_ids and self.selected_model_id in self.models:
            initial_ids = (self.selected_model_id,)
        self._set_selected_model_ids(initial_ids, active_model_id=self.selected_model_id)
        self._validate_invariants()

    @property
    def model_count(self) -> int:
        """현재 카탈로그의 모델 개수를 반환합니다."""
        return len(self.model_order)

    @property
    def selected_model(self) -> ModelCatalogRecord | None:
        """삭제·단축키가 사용할 마지막 클릭 레이어를 반환합니다."""
        return self.models.get(self.selected_model_id)

    @property
    def selected_reference_model(self) -> ModelCatalogRecord | None:
        """A 역할 선택이 정확히 하나일 때만 직접 Align 기준 레이어를 반환합니다."""

        record = self.models.get(self.selected_reference_model_id)
        if record is None or record.registration_status != MODEL_STATUS_REGISTERED:
            return None
        return record

    @property
    def selected_inactive_model(self) -> ModelCatalogRecord | None:
        """현재 다중 선택에서 P가 정확히 하나일 때 마커가 사용할 레이어를 반환합니다."""

        record = self.models.get(self.selected_moving_model_id)
        if record is None or record.registration_status not in _INACTIVE_MODEL_STATUSES:
            return None
        return record

    @property
    def selected_alignment_moving_model(self) -> ModelCatalogRecord | None:
        """P 역할 선택이 정확히 하나일 때만 직접 Align 이동 대상 레이어를 반환합니다."""

        record = self.models.get(self.selected_moving_model_id)
        if record is None or record.registration_status not in _INACTIVE_MODEL_STATUSES:
            return None
        return record

    @property
    def alignment_selection_is_complete(self) -> bool:
        """직접 Align에 필요한 A 하나 이상과 P 하나가 선택됐는지 반환합니다."""

        return bool(
            self.selected_reference_model_ids
            and self.selected_alignment_moving_model is not None
        )

    @property
    def alignment_selected_model_ids(self) -> tuple[str, ...]:
        """직접 Align 조건을 만족할 때 선택한 A 하나 이상과 P 하나의 ID를 반환합니다."""

        return self.selected_model_ids if self.alignment_selection_is_complete else ()

    @property
    def selected_reference_model_ids(self) -> tuple[str, ...]:
        """현재 다중 선택 중 REGISTERED A 역할 모델만 카탈로그 순서로 반환합니다."""

        return tuple(
            model_id
            for model_id in self.selected_model_ids
            if self.models[model_id].registration_status == MODEL_STATUS_REGISTERED
        )

    @property
    def selected_moving_model_ids(self) -> tuple[str, ...]:
        """현재 다중 선택 중 PENDING 계열 P 역할 모델만 카탈로그 순서로 반환합니다."""

        return tuple(
            model_id
            for model_id in self.selected_model_ids
            if self.models[model_id].registration_status in _INACTIVE_MODEL_STATUSES
        )

    @property
    def selected_inactive_model_id(self) -> str | None:
        """기존 조회 경계에 선택된 P 그룹 모델 ID만 노출합니다."""
        selected = self.selected_inactive_model
        return selected.model_id if selected is not None else None

    @property
    def internal_anchor_model(self) -> ModelCatalogRecord | None:
        """활성 그룹의 내부 좌표계 기준 Anchor 모델을 반환합니다."""
        return next(
            (
                model
                for model in self.active_models()
                if model.is_anchor
            ),
            None,
        )

    def ordered_models(self) -> tuple[ModelCatalogRecord, ...]:
        """가져온 순서를 보존한 전체 모델 목록을 반환합니다."""
        return tuple(
            self.models[model_id]
            for model_id in self.model_order
            if model_id in self.models
        )

    def active_models(self) -> tuple[ModelCatalogRecord, ...]:
        """활성 그룹에 속한 모델을 가져온 순서대로 반환합니다."""
        return tuple(
            model
            for model in self.ordered_models()
            if model.registration_status == MODEL_STATUS_REGISTERED
        )

    def inactive_models(self) -> tuple[ModelCatalogRecord, ...]:
        """아직 활성 그룹에 편입되지 않은 모델을 가져온 순서대로 반환합니다."""
        return tuple(
            model
            for model in self.ordered_models()
            if model.registration_status in _INACTIVE_MODEL_STATUSES
        )

    def registered_models(self) -> tuple[ModelCatalogRecord, ...]:
        """기존 내부 호출 호환을 위해 활성 모델을 반환합니다."""
        return self.active_models()

    def pending_models(self) -> tuple[ModelCatalogRecord, ...]:
        """기존 내부 호출 호환을 위해 비활성 모델을 반환합니다."""
        return self.inactive_models()

    def _set_selected_model_ids(
        self,
        model_ids: tuple[str, ...] | list[str] | set[str],
        *,
        active_model_id: str | None = None,
    ) -> None:
        """선택 집합을 카탈로그 순서로 고정하고 활성 행·범위 기준·A/P 파생값을 함께 갱신합니다."""

        requested = {str(model_id).strip() for model_id in model_ids if str(model_id).strip()}
        if not requested.issubset(self.models):
            raise ValueError("선택한 데이터 레이어가 카탈로그에 없습니다.")
        ordered_ids = tuple(model_id for model_id in self.model_order if model_id in requested)
        requested_active = str(active_model_id).strip() if active_model_id else ""
        if requested_active and requested_active not in ordered_ids:
            requested_active = ""
        self.selected_model_ids = ordered_ids
        self.selected_model_id = requested_active or (ordered_ids[-1] if ordered_ids else None)
        reference_ids = tuple(
            model_id
            for model_id in ordered_ids
            if self.models[model_id].registration_status == MODEL_STATUS_REGISTERED
        )
        moving_ids = tuple(
            model_id
            for model_id in ordered_ids
            if self.models[model_id].registration_status in _INACTIVE_MODEL_STATUSES
        )
        self.selected_reference_model_id = reference_ids[0] if len(reference_ids) == 1 else None
        self.selected_moving_model_id = moving_ids[0] if len(moving_ids) == 1 else None
        if not ordered_ids:
            self.selection_anchor_model_id = None
        elif len(ordered_ids) == 1:
            self.selection_anchor_model_id = ordered_ids[0]
        elif self.selection_anchor_model_id not in self.models:
            self.selection_anchor_model_id = self.selected_model_id

    def reset_demo_catalog(self) -> None:
        """테스트 fixture가 요청한 결정적 Demo 카탈로그만 명시적으로 구성합니다."""
        records = (
            ModelCatalogRecord(
                model_id=DEMO_MODEL_1_ID,
                display_name="Demo_1",
                load_order=1,
                registration_status=MODEL_STATUS_REGISTERED,
                is_anchor=True,
            ),
            ModelCatalogRecord(
                model_id=DEMO_MODEL_2_ID,
                display_name="Demo_2",
                load_order=2,
                registration_status=MODEL_STATUS_REGISTERED,
            ),
            ModelCatalogRecord(
                model_id=DEMO_MODEL_3_ID,
                display_name="Demo_3",
                load_order=3,
                registration_status=MODEL_STATUS_PENDING,
            ),
            ModelCatalogRecord(
                model_id=DEMO_MODEL_4_ID,
                display_name="Demo_4",
                load_order=4,
                registration_status=MODEL_STATUS_PENDING,
            ),
        )
        self.models = {record.model_id: record for record in records}
        self.model_order = [record.model_id for record in records]
        self._set_selected_model_ids(())
        self.message = ""
        self._validate_invariants()

    def add_imported_model(
        self,
        model_id: str,
        display_name: str,
        *,
        select_after_import: bool,
        geometry_kind: str = GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    ) -> ModelCatalogRecord:
        """검증을 마친 실제 모델을 첫 Anchor 또는 이후 PENDING 레이어로 추가합니다."""
        normalized_id = str(model_id).strip()
        normalized_name = str(display_name).strip()
        if not normalized_id:
            raise ValueError("가져온 COLMAP 모델 ID가 비어 있습니다.")
        if not normalized_name:
            raise ValueError("가져온 COLMAP 모델 표시 이름이 비어 있습니다.")
        if normalized_id in self.models:
            raise ValueError(f"중복 COLMAP 모델 ID입니다: {normalized_id}")
        is_first_model = not self.models
        record = ModelCatalogRecord(
            model_id=normalized_id,
            display_name=normalized_name,
            load_order=max(
                (existing.load_order for existing in self.models.values()),
                default=0,
            )
            + 1,
            registration_status=(
                MODEL_STATUS_REGISTERED if is_first_model else MODEL_STATUS_PENDING
            ),
            is_anchor=is_first_model,
            alignment_provenance=(
                AlignmentProvenanceRecord(
                    model_id=normalized_id,
                    method="ANCHOR",
                )
                if is_first_model
                else None
            ),
            geometry_kind=geometry_kind,
        )
        self.models[record.model_id] = record
        self.model_order.append(record.model_id)
        if not is_first_model and select_after_import:
            self._set_selected_model_ids((record.model_id,), active_model_id=record.model_id)
        self.message = f"{record.display_name} 데이터 레이어를 추가했습니다."
        self._validate_invariants()
        return record

    def remove_model(self, model_id: str) -> ModelCatalogRecord | None:
        """모델 하나를 제거하고 선택과 내부 Anchor 불변조건을 함께 복구합니다."""
        normalized_id = str(model_id).strip()
        removed = self.models.get(normalized_id)
        if removed is None:
            self.message = "삭제할 데이터 레이어를 찾을 수 없습니다."
            return None

        removed_index = self.model_order.index(normalized_id)
        records = dict(self.models)
        model_order = list(self.model_order)
        records.pop(normalized_id)
        model_order.pop(removed_index)

        selected_ids = tuple(
            model_id for model_id in self.selected_model_ids if model_id != normalized_id
        )
        selected_model_id = self.selected_model_id
        if selected_model_id == normalized_id:
            selected_model_id = selected_ids[-1] if selected_ids else None

        if removed.is_anchor and records:
            replacement_id = next(
                (
                    candidate_id
                    for candidate_id in model_order
                    if records[candidate_id].registration_status == MODEL_STATUS_REGISTERED
                ),
                model_order[0],
            )
            replacement = records[replacement_id]
            records[replacement_id] = replace(
                replacement,
                registration_status=MODEL_STATUS_REGISTERED,
                is_anchor=True,
            )

        self._validate_snapshot(
            records,
            model_order,
            selected_model_id,
            None,
            None,
            selected_ids,
        )
        self.models = records
        self.model_order = model_order
        self.selected_model_ids = ()
        self.selected_model_id = None
        self.selected_reference_model_id = None
        self.selected_moving_model_id = None
        self._set_selected_model_ids(selected_ids, active_model_id=selected_model_id)
        self.message = f"{removed.display_name} 데이터 레이어를 메모리에서 삭제했습니다."
        return removed

    def clear(self) -> None:
        """파일 교체와 등록 해제 전에 UI 카탈로그 참조를 비웁니다."""
        self.models.clear()
        self.model_order.clear()
        self._set_selected_model_ids(())
        self.message = ""

    def has_preview(self) -> bool:
        """정합 엔진이 남긴 미리보기 상태가 있는지 반환합니다."""
        return any(
            model.registration_status == MODEL_STATUS_PREVIEW
            for model in self.models.values()
        )

    def can_select_model(
        self,
        model_id: str,
        *,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """마커·미리보기가 없을 때 A/P 역할별 레이어 선택을 허용합니다."""
        record = self.models.get(model_id)
        if record is None:
            return False
        if model_id == self.selected_model_id:
            return True
        return not has_marker_draft and completed_pair_count == 0 and not self.has_preview()

    def select_model(
        self,
        model_id: str,
        *,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """일반 클릭으로 해당 레이어 하나만 선택하고 활성 행을 같은 레이어로 고정합니다."""
        record = self.models.get(model_id)
        if record is None:
            self.message = "선택할 데이터 레이어를 찾을 수 없습니다."
            return False
        if self.selected_model_ids == (model_id,) and self.selected_model_id == model_id:
            self.message = ""
            return True
        if has_marker_draft:
            self.message = "열린 마커 입력을 취소한 뒤 데이터 레이어를 변경하십시오."
            return False
        if completed_pair_count:
            self.message = "완료된 마커 쌍을 모두 정리한 뒤 데이터 레이어를 변경하십시오."
            return False
        if self.has_preview():
            self.message = "정합 미리보기를 취소한 뒤 데이터 레이어를 변경하십시오."
            return False
        self._set_selected_model_ids((model_id,), active_model_id=model_id)
        self._validate_invariants()
        self.message = f"{record.display_name} 데이터 레이어를 선택했습니다."
        return True

    def toggle_alignment_selection(
        self,
        model_id: str,
        *,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """Ctrl 클릭으로 한 레이어를 공통 다중 선택에서 추가하거나 해제합니다."""

        record = self.models.get(model_id)
        if record is None:
            self.message = "선택할 데이터 레이어를 찾을 수 없습니다."
            return False
        if (
            record.registration_status != MODEL_STATUS_REGISTERED
            and record.registration_status not in _INACTIVE_MODEL_STATUSES
        ):
            self.message = "현재 상태의 레이어는 정렬 대상으로 선택할 수 없습니다."
            return False
        if has_marker_draft:
            self.message = "열린 마커 입력을 취소한 뒤 정렬 대상을 변경하십시오."
            return False
        if completed_pair_count:
            self.message = "완료된 마커 쌍을 모두 정리한 뒤 정렬 대상을 변경하십시오."
            return False
        if self.has_preview():
            self.message = "정합 미리보기를 취소한 뒤 정렬 대상을 변경하십시오."
            return False

        selected_ids = set(self.selected_model_ids)
        was_selected = model_id in selected_ids
        if was_selected:
            selected_ids.remove(model_id)
            active_model_id = self.selected_model_id if self.selected_model_id != model_id else None
        else:
            selected_ids.add(model_id)
            active_model_id = model_id
        self._set_selected_model_ids(selected_ids, active_model_id=active_model_id)
        self.selection_anchor_model_id = model_id
        self._validate_invariants()
        role = "A" if record.registration_status == MODEL_STATUS_REGISTERED else "P"
        state = "해제" if was_selected else "추가"
        self.message = f"{record.display_name} 레이어를 Ctrl {role} 복수 선택에서 {state}했습니다."
        return True

    def select_model_range(
        self,
        model_id: str,
        *,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """Shift 클릭으로 기준 레이어부터 클릭 레이어까지 표시 순서의 연속 범위를 선택합니다."""

        record = self.models.get(model_id)
        if record is None:
            self.message = "선택할 데이터 레이어를 찾을 수 없습니다."
            return False
        if has_marker_draft:
            self.message = "열린 마커 입력을 취소한 뒤 정렬 대상을 변경하십시오."
            return False
        if completed_pair_count:
            self.message = "완료된 마커 쌍을 모두 정리한 뒤 정렬 대상을 변경하십시오."
            return False
        if self.has_preview():
            self.message = "정합 미리보기를 취소한 뒤 정렬 대상을 변경하십시오."
            return False

        display_order = tuple(
            item.model_id
            for item in (*self.active_models(), *self.inactive_models())
        )
        anchor_model_id = self.selection_anchor_model_id
        if anchor_model_id not in display_order:
            anchor_model_id = self.selected_model_id if self.selected_model_id in display_order else model_id
        anchor_index = display_order.index(anchor_model_id)
        target_index = display_order.index(model_id)
        range_start = min(anchor_index, target_index)
        range_end = max(anchor_index, target_index) + 1
        selected_ids = display_order[range_start:range_end]
        self._set_selected_model_ids(selected_ids, active_model_id=model_id)
        self.selection_anchor_model_id = anchor_model_id
        self._validate_invariants()
        self.message = (
            f"{self.models[anchor_model_id].display_name}부터 {record.display_name}까지 "
            f"{len(selected_ids)}개 레이어를 Shift 범위 선택했습니다."
        )
        return True

    def clear_selected_model(self) -> bool:
        """세션 종료 전에 다중 선택·활성 행·역할별 파생 선택을 모두 비웁니다."""

        changed = any(
            model_id is not None
            for model_id in (self.selected_model_id, *self.selected_model_ids)
        )
        self._set_selected_model_ids(())
        return changed

    def clear_alignment_selection(self) -> bool:
        """3점 마커 모드로 돌아갈 때 활성 행 하나만 남기고 다중 선택을 해제합니다."""

        active_model_id = self.selected_model_id
        target_ids = (active_model_id,) if active_model_id in self.models else ()
        changed = self.selected_model_ids != target_ids
        self._set_selected_model_ids(target_ids, active_model_id=active_model_id)
        return changed

    def can_transfer_model_group(
        self,
        model_id: str,
        *,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """마커·미리보기가 없고 마지막 A가 아닐 때 A/P 그룹 전환을 허용합니다."""
        record = self.models.get(model_id)
        if record is None or has_marker_draft or completed_pair_count or self.has_preview():
            return False
        if record.registration_status == MODEL_STATUS_REGISTERED:
            return len(self.active_models()) > 1
        return record.registration_status in _INACTIVE_MODEL_STATUSES

    def transfer_model_group(
        self,
        model_id: str,
        *,
        source: str,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """수동 더블클릭으로 A↔P를 전환하고 전환 레이어 하나만 선택으로 남깁니다."""
        if source not in _VALID_MODEL_ACTIVATION_SOURCES:
            raise ValueError("지원하지 않는 데이터 레이어 활성화 출처입니다.")
        record = self.models.get(model_id)
        if record is None:
            self.message = "이동할 데이터 레이어를 찾을 수 없습니다."
            return False
        if has_marker_draft:
            self.message = "열린 마커 입력을 취소한 뒤 레이어 그룹을 변경하십시오."
            return False
        if completed_pair_count:
            self.message = "완료된 마커 쌍을 모두 정리한 뒤 레이어 그룹을 변경하십시오."
            return False
        if self.has_preview():
            self.message = "정합 미리보기를 취소한 뒤 레이어 그룹을 변경하십시오."
            return False
        if record.registration_status == MODEL_STATUS_REGISTERED:
            active_models = self.active_models()
            if len(active_models) <= 1:
                self.message = "A 그룹에는 최소 한 개의 레이어가 필요합니다."
                return False
            if record.is_anchor:
                replacement_anchor = next(
                    model for model in active_models if model.model_id != model_id
                )
                self.models[replacement_anchor.model_id] = replace(
                    replacement_anchor,
                    is_anchor=True,
                )
            self.models[model_id] = replace(
                record,
                registration_status=MODEL_STATUS_PENDING,
                is_anchor=False,
            )
            target_group = "P"
        elif record.registration_status in _INACTIVE_MODEL_STATUSES:
            self.models[model_id] = replace(
                record,
                registration_status=MODEL_STATUS_REGISTERED,
                is_anchor=False,
            )
            target_group = "A"
        else:
            self.message = "현재 상태의 레이어는 그룹을 변경할 수 없습니다."
            return False
        # 그룹이 바뀐 뒤 기존 다중 선택을 유지하면 역할별 변환 대상이 모호해지므로 전환 행만 남깁니다.
        self._set_selected_model_ids((model_id,), active_model_id=model_id)
        self._validate_invariants()
        self.message = f"{record.display_name} 레이어를 {target_group} 그룹으로 이동했습니다."
        return True

    def promote_model_to_active(
        self,
        model_id: str,
        *,
        source: str,
        has_marker_draft: bool,
        completed_pair_count: int,
    ) -> bool:
        """후속 정합 승인 경로에서 P 레이어만 A 그룹으로 편입합니다."""
        record = self.models.get(model_id)
        if record is None or record.registration_status == MODEL_STATUS_REGISTERED:
            self.message = "A 그룹으로 이동할 P 레이어가 아닙니다."
            return False
        return self.transfer_model_group(
            model_id,
            source=source,
            has_marker_draft=has_marker_draft,
            completed_pair_count=completed_pair_count,
        )

    def promote_model_for_icp_commit(self, model_id: str) -> None:
        """검증된 ICP 승인에서만 P 레이어를 A 그룹으로 원자 편입합니다."""
        normalized_id = str(model_id).strip()
        record = self.models.get(normalized_id)
        if record is None:
            raise ValueError("ICP 승인할 P 데이터 레이어를 찾을 수 없습니다.")
        if record.registration_status == MODEL_STATUS_REGISTERED:
            raise ValueError("ICP 승인할 데이터 레이어가 이미 A 그룹에 있습니다.")
        if record.registration_status not in _INACTIVE_MODEL_STATUSES:
            raise ValueError("현재 데이터 레이어 상태는 ICP 승인으로 편입할 수 없습니다.")
        updated_records = dict(self.models)
        updated_records[normalized_id] = replace(
            record,
            registration_status=MODEL_STATUS_REGISTERED,
            is_anchor=False,
        )
        selected_ids = tuple(
            model_id for model_id in self.selected_model_ids if model_id != normalized_id
        )
        selected_model_id = (
            None if self.selected_model_id == normalized_id else self.selected_model_id
        )
        selected_reference_ids = tuple(
            model_id
            for model_id in selected_ids
            if updated_records[model_id].registration_status == MODEL_STATUS_REGISTERED
        )
        selected_moving_ids = tuple(
            model_id
            for model_id in selected_ids
            if updated_records[model_id].registration_status in _INACTIVE_MODEL_STATUSES
        )
        self._validate_snapshot(
            updated_records,
            list(self.model_order),
            selected_model_id,
            selected_reference_ids[0] if len(selected_reference_ids) == 1 else None,
            selected_moving_ids[0] if len(selected_moving_ids) == 1 else None,
            selected_ids,
        )
        self.models = updated_records
        self.selected_model_ids = ()
        self.selected_model_id = None
        self.selected_reference_model_id = None
        self.selected_moving_model_id = None
        self._set_selected_model_ids(selected_ids, active_model_id=selected_model_id)
        self.message = f"{record.display_name} 레이어를 ICP 정렬 결과로 A 그룹에 편입했습니다."

    def set_alignment_provenance(
        self,
        model_id: str,
        provenance: AlignmentProvenanceRecord | None,
    ) -> None:
        """확정 모델의 provenance를 기록하거나 Undo 복원에서 명시적으로 비웁니다."""

        normalized_id = str(model_id).strip()
        record = self.models.get(normalized_id)
        if record is None:
            raise ValueError("정렬 provenance를 기록할 데이터 레이어를 찾을 수 없습니다.")
        if provenance is not None and not isinstance(provenance, AlignmentProvenanceRecord):
            raise TypeError("정렬 provenance 형식이 올바르지 않습니다.")
        if provenance is not None and provenance.model_id != normalized_id:
            raise ValueError("정렬 provenance 모델 ID가 Data Layers와 다릅니다.")
        self.models[normalized_id] = replace(
            record,
            alignment_provenance=provenance,
        )

    def set_model_visibility(self, model_id: str, is_visible: bool) -> bool:
        """지정 레이어의 GPU 표시 여부만 독립적으로 변경합니다."""
        record = self.models.get(model_id)
        if record is None:
            self.message = "표시 상태를 변경할 데이터 레이어를 찾을 수 없습니다."
            return False
        visible = bool(is_visible)
        if record.is_visible == visible:
            self.message = ""
            return True
        self.models[model_id] = replace(record, is_visible=visible)
        state_text = "표시" if visible else "숨김"
        self.message = f"{record.display_name} 레이어를 {state_text} 상태로 변경했습니다."
        return True

    def toggle_model_visibility(self, model_id: str) -> bool:
        """지정 레이어의 현재 GPU 표시 상태를 반전합니다."""
        record = self.models.get(model_id)
        if record is None:
            self.message = "표시 상태를 변경할 데이터 레이어를 찾을 수 없습니다."
            return False
        return self.set_model_visibility(model_id, not record.is_visible)

    def snapshot(self) -> ModelCatalogSnapshot:
        """그룹 전환 Undo에 필요한 모델·다중 선택·표시 상태를 불변 복사합니다."""
        return ModelCatalogSnapshot(
            records=self.ordered_models(),
            model_order=tuple(self.model_order),
            selected_model_id=self.selected_model_id,
            selected_reference_model_id=self.selected_reference_model_id,
            selected_moving_model_id=self.selected_moving_model_id,
            selected_model_ids=self.selected_model_ids,
            selection_anchor_model_id=self.selection_anchor_model_id,
        )

    def restore_snapshot(self, snapshot: ModelCatalogSnapshot) -> None:
        """검증된 카탈로그 스냅샷을 한 번에 복원해 Undo 경계를 보장합니다."""
        if not isinstance(snapshot, ModelCatalogSnapshot):
            raise TypeError("데이터 레이어 Undo 스냅샷 형식이 올바르지 않습니다.")
        records = {record.model_id: record for record in snapshot.records}
        model_order = list(snapshot.model_order)
        self._validate_snapshot(
            records,
            model_order,
            snapshot.selected_model_id,
            snapshot.selected_reference_model_id,
            snapshot.selected_moving_model_id,
            snapshot.selected_model_ids,
            snapshot.selection_anchor_model_id,
        )
        self.models = records
        self.model_order = model_order
        self.selected_model_ids = ()
        self.selected_model_id = None
        self.selected_reference_model_id = None
        self.selected_moving_model_id = None
        self.selection_anchor_model_id = snapshot.selection_anchor_model_id
        self._set_selected_model_ids(
            snapshot.selected_model_ids,
            active_model_id=snapshot.selected_model_id,
        )

    def restore_group_snapshot(self, snapshot: ModelCatalogSnapshot) -> None:
        """그룹 Undo를 복원하되 이후 변경한 독립 표시 토글은 현재 값으로 보존합니다."""
        current_visibility = {
            model_id: record.is_visible
            for model_id, record in self.models.items()
        }
        self.restore_snapshot(snapshot)
        for model_id, is_visible in current_visibility.items():
            record = self.models.get(model_id)
            if record is not None and record.is_visible != is_visible:
                self.models[model_id] = replace(record, is_visible=is_visible)
        self._validate_invariants()

    def _validate_invariants(self) -> None:
        """현재 카탈로그의 다중 선택·파생 역할·Anchor 불변조건을 검증합니다."""
        self._validate_snapshot(
            self.models,
            self.model_order,
            self.selected_model_id,
            self.selected_reference_model_id,
            self.selected_moving_model_id,
            self.selected_model_ids,
            self.selection_anchor_model_id,
        )

    @staticmethod
    def _validate_snapshot(
        records: dict[str, ModelCatalogRecord],
        model_order: list[str],
        selected_model_id: str | None,
        selected_reference_model_id: str | None,
        selected_moving_model_id: str | None,
        selected_model_ids: tuple[str, ...] = (),
        selection_anchor_model_id: str | None = None,
    ) -> None:
        """Undo 복원 전에 카탈로그 스냅샷의 다중 선택·역할 구조를 검증합니다."""
        if set(model_order) != set(records) or len(model_order) != len(records):
            raise ValueError("데이터 레이어 순서와 모델 식별자가 일치하지 않습니다.")
        if selected_model_id is not None and selected_model_id not in records:
            raise ValueError("선택된 데이터 레이어가 카탈로그에 없습니다.")
        if selection_anchor_model_id is not None and selection_anchor_model_id not in records:
            raise ValueError("범위 선택 기준 데이터 레이어가 카탈로그에 없습니다.")
        if len(set(selected_model_ids)) != len(selected_model_ids):
            raise ValueError("선택된 데이터 레이어에 중복 모델이 있습니다.")
        if any(model_id not in records for model_id in selected_model_ids):
            raise ValueError("선택된 데이터 레이어가 카탈로그에 없습니다.")
        expected_selected_ids = tuple(model_id for model_id in model_order if model_id in selected_model_ids)
        if selected_model_ids != expected_selected_ids:
            raise ValueError("선택된 데이터 레이어 순서가 카탈로그 순서와 다릅니다.")
        if selected_model_id is not None and selected_model_id not in selected_model_ids:
            raise ValueError("활성 데이터 레이어가 다중 선택에 포함되지 않습니다.")
        expected_reference_ids = tuple(
            model_id
            for model_id in selected_model_ids
            if records[model_id].registration_status == MODEL_STATUS_REGISTERED
        )
        expected_moving_ids = tuple(
            model_id
            for model_id in selected_model_ids
            if records[model_id].registration_status in _INACTIVE_MODEL_STATUSES
        )
        if selected_reference_model_id != (
            expected_reference_ids[0] if len(expected_reference_ids) == 1 else None
        ):
            raise ValueError("선택된 A 데이터 레이어 파생값이 다중 선택과 다릅니다.")
        if selected_moving_model_id != (
            expected_moving_ids[0] if len(expected_moving_ids) == 1 else None
        ):
            raise ValueError("선택된 P 데이터 레이어 파생값이 다중 선택과 다릅니다.")
        if selected_reference_model_id is not None:
            reference = records.get(selected_reference_model_id)
            if reference is None or reference.registration_status != MODEL_STATUS_REGISTERED:
                raise ValueError("선택된 A 데이터 레이어가 등록 그룹에 없습니다.")
        if selected_moving_model_id is not None:
            moving = records.get(selected_moving_model_id)
            if moving is None or moving.registration_status not in _INACTIVE_MODEL_STATUSES:
                raise ValueError("선택된 P 데이터 레이어가 이동 그룹에 없습니다.")
        if (
            selected_reference_model_id is not None
            and selected_reference_model_id == selected_moving_model_id
        ):
            raise ValueError("같은 데이터 레이어를 A와 P로 동시에 선택할 수 없습니다.")
        anchor_count = sum(
            1
            for record in records.values()
            if record.is_anchor and record.registration_status == MODEL_STATUS_REGISTERED
        )
        if records and anchor_count != 1:
            raise ValueError("활성 그룹에는 내부 기준 Anchor가 정확히 하나 있어야 합니다.")


@dataclass(frozen=True)
class SelectedPoint:
    """선택한 COLMAP 점·메시 정점·면 위치의 공통 원본 참조를 보관합니다."""

    model_role: str
    point3d_id: int | None = None
    local_position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    model_id: str = ""
    point_kind: str = MARKER_POINT_KIND_COLMAP
    source_point_id: int | None = None

    def __post_init__(self) -> None:
        """기존 point3d_id와 점·면 source ID 및 Source Local 좌표를 정규화합니다."""
        point_kind = str(self.point_kind).strip().upper()
        if point_kind not in MARKER_POINT_KINDS:
            raise ValueError("마커 point kind가 올바르지 않습니다.")
        source_id = self.source_point_id if self.source_point_id is not None else self.point3d_id
        if source_id is None:
            raise ValueError("마커 원본 점 ID가 비어 있습니다.")
        try:
            source_id = int(source_id)
        except (TypeError, ValueError) as error:
            raise ValueError("마커 원본 점 ID가 올바르지 않습니다.") from error
        if source_id < 0:
            raise ValueError("마커 원본 점 ID는 음수일 수 없습니다.")
        position = tuple(float(value) for value in self.local_position)
        if len(position) != 3 or not all(math.isfinite(value) for value in position):
            raise ValueError("마커 Source Local 좌표는 유한한 길이 3 값이어야 합니다.")
        object.__setattr__(self, "point_kind", point_kind)
        object.__setattr__(self, "source_point_id", source_id)
        # 명시적으로 비운 구형 point3d_id는 보존해 저장 마커 검증이 누락을 감지하게 합니다.
        if self.point3d_id is not None:
            object.__setattr__(self, "point3d_id", source_id)
        object.__setattr__(self, "local_position", position)


@dataclass(frozen=True)
class MarkerPair:
    """확정된 A/P 대응쌍과 재사용하지 않는 안정 식별자를 보관합니다."""

    pair_id: int
    reference: SelectedPoint
    moving: SelectedPoint


@dataclass(frozen=True)
class MarkerCommittedSnapshot:
    """드래프트 시작 전 확정 상태를 취소 복원용으로 보관합니다."""

    next_pair_id: int
    active_pair_id: int | None
    completed_pairs: tuple[MarkerPair, ...]
    message: str


@dataclass(frozen=True)
class MarkerDraft:
    """확정 전 A/P 후보와 편집 시작 기준 스냅샷을 보관합니다."""

    mode: str
    pair_id: int
    source_pair_id: int | None
    reference: SelectedPoint | None
    moving: SelectedPoint | None
    baseline_snapshot: MarkerCommittedSnapshot


@dataclass(frozen=True)
class MarkerSelectionSnapshot:
    """세션 Undo가 원자적으로 복원할 전체 마커 선택 상태입니다."""

    phase: str
    next_pair_id: int
    active_pair_id: int | None
    completed_pairs: tuple[MarkerPair, ...]
    draft: MarkerDraft | None
    message: str


@dataclass
class MarkerSelectionState:
    """생성·편집 드래프트와 확정 마커 쌍을 분리해 관리합니다."""

    phase: str = PHASE_IDLE
    next_pair_id: int = 1
    active_pair_id: int | None = None
    completed_pairs: list[MarkerPair] = field(default_factory=list)
    draft: MarkerDraft | None = None
    message: str = ""

    @property
    def has_draft(self) -> bool:
        """확정 전 입력 범위가 열린 상태인지 반환합니다."""
        return self.phase == PHASE_CAPTURING and self.draft is not None

    @property
    def active_pair(self) -> MarkerPair | None:
        """현재 UI에서 선택한 확정 마커 쌍을 반환합니다."""
        if self.active_pair_id is None:
            return None
        return self.get_pair(self.active_pair_id)

    def snapshot(self) -> MarkerSelectionSnapshot:
        """현재 마커 상태를 세션 Undo용 불변 스냅샷으로 복사합니다."""
        return MarkerSelectionSnapshot(
            phase=self.phase,
            next_pair_id=self.next_pair_id,
            active_pair_id=self.active_pair_id,
            completed_pairs=tuple(self.completed_pairs),
            draft=self.draft,
            message=self.message,
        )

    def restore_snapshot(self, snapshot: MarkerSelectionSnapshot) -> None:
        """검증된 스냅샷을 한 번에 복원해 Undo 경계를 보장합니다."""
        if not isinstance(snapshot, MarkerSelectionSnapshot):
            raise TypeError("마커 Undo 스냅샷 형식이 올바르지 않습니다.")
        self.phase = snapshot.phase
        self.next_pair_id = snapshot.next_pair_id
        self.active_pair_id = snapshot.active_pair_id
        self.completed_pairs = list(snapshot.completed_pairs)
        self.draft = snapshot.draft
        self.message = snapshot.message

    def begin_create_draft(self) -> bool:
        """양방향 A/P 입력을 받을 새 마커 쌍 드래프트를 시작합니다."""
        if self.has_draft:
            self.message = "이미 편집 중인 마커 쌍이 있습니다. 먼저 확정하거나 취소하십시오."
            return False
        baseline = self._committed_snapshot()
        self.phase = PHASE_CAPTURING
        self.draft = MarkerDraft(
            mode=DRAFT_MODE_CREATE,
            pair_id=self.next_pair_id,
            source_pair_id=None,
            reference=None,
            moving=None,
            baseline_snapshot=baseline,
        )
        self.message = f"A{self.next_pair_id} 또는 P{self.next_pair_id} 점을 선택하십시오."
        return True

    def begin_edit_draft(self, pair_id: int) -> bool:
        """선택한 확정 쌍의 A/P 후보를 유지한 통합 편집 드래프트를 시작합니다."""
        if self.has_draft:
            self.message = "이미 편집 중인 마커 쌍이 있습니다."
            return False
        pair = self.get_pair(pair_id)
        if pair is None:
            self.message = "편집할 마커 쌍을 찾을 수 없습니다."
            return False
        baseline = self._committed_snapshot()
        self.phase = PHASE_CAPTURING
        self.active_pair_id = pair.pair_id
        self.draft = MarkerDraft(
            mode=DRAFT_MODE_EDIT,
            pair_id=pair.pair_id,
            source_pair_id=pair.pair_id,
            reference=pair.reference,
            moving=pair.moving,
            baseline_snapshot=baseline,
        )
        self.message = f"A{pair.pair_id} 또는 P{pair.pair_id} 점을 다시 선택해 마커 쌍을 편집하십시오."
        return True

    def select_draft_point(
        self,
        role: str,
        point3d_id: int,
        position: tuple[float, float, float],
        *,
        model_id: str | None = None,
        point_kind: str = MARKER_POINT_KIND_COLMAP,
        source_point_id: int | None = None,
    ) -> bool:
        """모델 소유권을 보존해 드래프트 후보를 바꾸고 중복 선택을 거부합니다."""
        rejection_message = self._draft_pick_rejection_message(
            role,
            point3d_id,
            position,
            model_id=model_id,
            point_kind=point_kind,
            source_point_id=source_point_id,
        )
        if rejection_message is not None:
            if rejection_message != "이미 선택한 점입니다.":
                self.message = rejection_message
            return False
        draft = self.draft
        if draft is None:
            return False
        selected = SelectedPoint(
            model_role=role,
            point3d_id=int(point3d_id),
            local_position=tuple(float(value) for value in position),
            model_id=str(model_id or ""),
            point_kind=point_kind,
            source_point_id=source_point_id if source_point_id is not None else point3d_id,
        )
        if role == ROLE_REFERENCE:
            self.draft = replace(
                draft,
                reference=selected,
            )
        else:
            self.draft = replace(
                draft,
                moving=selected,
            )
        self.message = self._draft_status_message(self.draft)
        return True

    def can_select_draft_point(
        self,
        role: str,
        point3d_id: int,
        position: tuple[float, float, float],
        *,
        model_id: str | None = None,
        point_kind: str = MARKER_POINT_KIND_COLMAP,
        source_point_id: int | None = None,
    ) -> bool:
        """모델을 포함한 점 후보가 드래프트를 바꾸는지 상태 변경 없이 확인합니다."""
        return self._draft_pick_rejection_message(
            role,
            point3d_id,
            position,
            model_id=model_id,
            point_kind=point_kind,
            source_point_id=source_point_id,
        ) is None

    def can_confirm_draft(self) -> bool:
        """신규 후보 완성 또는 기존 쌍의 실제 변경 여부를 상태 변경 없이 확인합니다."""
        draft = self.draft
        if (
            not self.has_draft
            or draft is None
            or draft.reference is None
            or draft.moving is None
        ):
            return False
        if draft.mode == DRAFT_MODE_CREATE:
            return True
        source_pair = self._pair_from_committed_snapshot(
            draft.baseline_snapshot,
            draft.source_pair_id,
        )
        return bool(
            source_pair is not None
            and (
                draft.reference != source_pair.reference
                or draft.moving != source_pair.moving
            )
        )

    def commit_draft(self) -> MarkerPair | None:
        """준비된 드래프트를 하나의 확정 마커 쌍으로 반영합니다."""
        draft = self.draft
        if not self.can_confirm_draft() or draft is None:
            if draft is not None and draft.mode == DRAFT_MODE_EDIT:
                self.message = "A 또는 P 점을 실제로 변경한 뒤 쌍 지정을 누르십시오."
            else:
                self.message = "A/P 후보를 모두 선택한 뒤 쌍 지정을 누르십시오."
            return None
        if draft.reference is None or draft.moving is None:
            return None
        pair = MarkerPair(
            pair_id=draft.pair_id,
            reference=draft.reference,
            moving=draft.moving,
        )
        if draft.mode == DRAFT_MODE_CREATE:
            self.completed_pairs.append(pair)
            self.next_pair_id = max(self.next_pair_id, pair.pair_id + 1)
            message = f"A{pair.pair_id} ↔ P{pair.pair_id} 대응쌍을 등록했습니다."
        else:
            source_pair_id = draft.source_pair_id
            list_index = self._find_pair_list_index(source_pair_id)
            if list_index is None:
                self.message = "편집할 기존 마커 쌍을 찾을 수 없습니다."
                return None
            self.completed_pairs[list_index] = pair
            message = f"A{pair.pair_id} ↔ P{pair.pair_id} 대응쌍을 편집했습니다."
        self.phase = PHASE_IDLE
        self.draft = None
        self.active_pair_id = pair.pair_id
        self.message = message
        return pair

    def cancel_draft(self) -> bool:
        """드래프트 시작 전 확정 상태로 돌아가며 미확정 입력을 버립니다."""
        draft = self.draft
        if not self.has_draft or draft is None:
            return False
        self._restore_committed_snapshot(draft.baseline_snapshot)
        self.phase = PHASE_IDLE
        self.draft = None
        self.message = "마커 쌍 입력을 취소했습니다."
        return True

    def delete_active_pair(self) -> MarkerPair | None:
        """선택한 확정 쌍을 삭제하고 다음 또는 이전 쌍으로 선택을 옮깁니다."""
        if self.has_draft:
            self.message = "편집 중에는 마커 쌍을 삭제할 수 없습니다."
            return None
        list_index = self._find_pair_list_index(self.active_pair_id)
        if list_index is None:
            self.message = "삭제할 마커 쌍을 선택하십시오."
            return None
        removed = self.completed_pairs.pop(list_index)
        if self.completed_pairs:
            fallback_index = min(list_index, len(self.completed_pairs) - 1)
            self.active_pair_id = self.completed_pairs[fallback_index].pair_id
        else:
            self.active_pair_id = None
        self.message = f"A{removed.pair_id} ↔ P{removed.pair_id} 대응쌍을 삭제했습니다."
        return removed

    def remove_pairs_for_model(self, model_id: str) -> int:
        """지정 모델을 참조하는 확정 마커 쌍을 모두 제거하고 선택을 보정합니다."""
        if self.has_draft:
            raise RuntimeError("열린 마커 입력 중에는 모델 마커를 정리할 수 없습니다.")
        normalized_id = str(model_id).strip()
        if not normalized_id:
            raise ValueError("마커를 정리할 모델 ID가 비어 있습니다.")

        original_pairs = tuple(self.completed_pairs)
        removed_indices = [
            index
            for index, pair in enumerate(original_pairs)
            if pair.reference.model_id == normalized_id
            or pair.moving.model_id == normalized_id
        ]
        if not removed_indices:
            return 0

        removed_pair_ids = {original_pairs[index].pair_id for index in removed_indices}
        self.completed_pairs = [
            pair for pair in original_pairs if pair.pair_id not in removed_pair_ids
        ]
        if self.active_pair_id in removed_pair_ids:
            if self.completed_pairs:
                fallback_index = min(removed_indices[0], len(self.completed_pairs) - 1)
                self.active_pair_id = self.completed_pairs[fallback_index].pair_id
            else:
                self.active_pair_id = None
        self.message = f"모델과 연결된 마커 쌍 {len(removed_indices)}개를 정리했습니다."
        return len(removed_indices)

    def set_active_pair_id(self, pair_id: int | None) -> bool:
        """UIList가 선택한 안정 쌍 식별자를 런타임 상태에 반영합니다."""
        if pair_id is None or int(pair_id) <= 0:
            changed = self.active_pair_id is not None
            self.active_pair_id = None
            return changed
        pair = self.get_pair(int(pair_id))
        if pair is None:
            return False
        changed = self.active_pair_id != pair.pair_id
        self.active_pair_id = pair.pair_id
        return changed

    def get_pair(self, pair_id: int | None) -> MarkerPair | None:
        """안정 식별자로 확정 마커 쌍을 조회합니다."""
        if pair_id is None:
            return None
        list_index = self._find_pair_list_index(pair_id)
        return self.completed_pairs[list_index] if list_index is not None else None

    def is_point_used(
        self,
        role: str,
        point3d_id: int,
        *,
        model_id: str | None = None,
        exclude_pair_id: int | None = None,
        point_kind: str = MARKER_POINT_KIND_COLMAP,
        position: tuple[float, float, float] | None = None,
    ) -> bool:
        """다른 쌍이 같은 모델의 원본 점 또는 같은 면 위치를 쓰는지 확인합니다."""
        if role not in _VALID_ROLES:
            return False
        normalized_model_id = str(model_id or "")
        for pair in self.completed_pairs:
            if pair.pair_id == exclude_pair_id:
                continue
            point = pair.reference if role == ROLE_REFERENCE else pair.moving
            stored_id = point.source_point_id if point.source_point_id is not None else point.point3d_id
            if point.point_kind != point_kind or stored_id != point3d_id:
                continue
            if point_kind == MARKER_POINT_KIND_MESH_SURFACE and position is not None:
                candidate_position = tuple(float(value) for value in position)
                if len(candidate_position) != 3 or any(
                    abs(stored - candidate) > MARKER_ALIGNMENT_POSITION_TOLERANCE
                    for stored, candidate in zip(
                        point.local_position,
                        candidate_position,
                        strict=True,
                    )
                ):
                    continue
            if (
                not normalized_model_id
                or not point.model_id
                or point.model_id == normalized_model_id
            ):
                return True
        return False

    def clear(self) -> None:
        """세션 종료 또는 파일 교체 전에 마커 상태를 초기화합니다."""
        self.phase = PHASE_IDLE
        self.next_pair_id = 1
        self.active_pair_id = None
        self.completed_pairs.clear()
        self.draft = None
        self.message = ""

    def _committed_snapshot(self) -> MarkerCommittedSnapshot:
        """현재 확정 마커 목록만 취소용 불변 상태로 복사합니다."""
        return MarkerCommittedSnapshot(
            next_pair_id=self.next_pair_id,
            active_pair_id=self.active_pair_id,
            completed_pairs=tuple(self.completed_pairs),
            message=self.message,
        )

    def _restore_committed_snapshot(self, snapshot: MarkerCommittedSnapshot) -> None:
        """드래프트 취소를 위해 확정 상태만 복원합니다."""
        self.next_pair_id = snapshot.next_pair_id
        self.active_pair_id = snapshot.active_pair_id
        self.completed_pairs = list(snapshot.completed_pairs)

    def _find_pair_list_index(self, pair_id: int | None) -> int | None:
        """확정 목록에서 안정 식별자의 배열 위치를 찾습니다."""
        if pair_id is None:
            return None
        for index, pair in enumerate(self.completed_pairs):
            if pair.pair_id == pair_id:
                return index
        return None

    @staticmethod
    def _pair_from_committed_snapshot(
        snapshot: MarkerCommittedSnapshot,
        pair_id: int | None,
    ) -> MarkerPair | None:
        """드래프트 시작 시점의 확정 쌍을 찾아 실제 변경 비교 기준으로 반환합니다."""
        if pair_id is None:
            return None
        for pair in snapshot.completed_pairs:
            if pair.pair_id == pair_id:
                return pair
        return None

    def _draft_pick_rejection_message(
        self,
        role: str,
        point3d_id: int,
        position: tuple[float, float, float],
        *,
        model_id: str | None = None,
        point_kind: str = MARKER_POINT_KIND_COLMAP,
        source_point_id: int | None = None,
    ) -> str | None:
        """모델 소유권을 포함한 점 선택의 거부 이유를 상태 변경 없이 반환합니다."""
        draft = self.draft
        if not self.has_draft or draft is None:
            return "먼저 마커 쌍 추가 또는 편집을 시작하십시오."
        if role not in _VALID_ROLES:
            return "마커 역할이 올바르지 않습니다."
        if self.is_point_used(
            role,
            int(source_point_id if source_point_id is not None else point3d_id),
            model_id=model_id,
            exclude_pair_id=draft.source_pair_id,
            point_kind=point_kind,
            position=position,
        ):
            return "이미 다른 마커 쌍에 사용한 점입니다."
        selected = SelectedPoint(
            model_role=role,
            point3d_id=int(point3d_id),
            local_position=tuple(float(value) for value in position),
            model_id=str(model_id or ""),
            point_kind=point_kind,
            source_point_id=source_point_id if source_point_id is not None else point3d_id,
        )
        current = draft.reference if role == ROLE_REFERENCE else draft.moving
        if current == selected:
            return "이미 선택한 점입니다."
        return None

    def _draft_status_message(self, draft: MarkerDraft) -> str:
        """현재 후보 입력 상태를 N 패널과 화면 안내에 표시할 문장으로 만듭니다."""
        reference_status = "완료" if draft.reference is not None else "대기"
        moving_status = "완료" if draft.moving is not None else "대기"
        if self.can_confirm_draft():
            if draft.mode == DRAFT_MODE_EDIT:
                return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · 변경됨 · 쌍 지정을 누르십시오."
            return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · 쌍 지정을 누르십시오."
        if draft.mode == DRAFT_MODE_EDIT:
            return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · A 또는 P 점을 실제로 변경하십시오."
        return f"A{draft.pair_id} {reference_status} · P{draft.pair_id} {moving_status} · 점을 계속 선택할 수 있습니다."


@dataclass
class SessionState:
    """COLMAP Align 작업공간 전환과 입력 가드 생명주기를 보관합니다."""

    phase: str = SESSION_INACTIVE
    session_active: bool = False
    target_window_ref: Any = None
    target_window_pointer: int = 0
    align_workspace_ref: Any = None
    align_workspace_pointer: int = 0
    align_workspace_name: str = ""
    source_workspace_ref: Any = None
    source_workspace_pointer: int = 0
    source_workspace_name: str = ""
    recovery_pending: bool = False
    exit_requested: bool = False
    input_guard_active: bool = False
    input_guard_window_pointer: int = 0
    generation: int = 0
    message: str = ""

    def begin_entering(self, window: Any, source_workspace: Any) -> None:
        """전환 대상 창과 원래 작업공간을 저장하고 진입 상태로 바꿉니다."""
        self.generation += 1
        self.phase = SESSION_ENTERING
        self.session_active = False
        self.target_window_ref = window
        self.target_window_pointer = window.as_pointer()
        self.source_workspace_ref = source_workspace
        self.source_workspace_pointer = source_workspace.as_pointer()
        self.source_workspace_name = source_workspace.name
        self.align_workspace_ref = None
        self.align_workspace_pointer = 0
        self.align_workspace_name = ""
        self.recovery_pending = False
        self.exit_requested = False
        self.input_guard_active = False
        self.input_guard_window_pointer = 0
        self.message = "COLMAP Align 작업공간을 준비 중입니다."

    def set_align_workspace(self, workspace: Any) -> None:
        """구성된 COLMAP Align 작업공간 참조와 포인터를 저장합니다."""
        self.align_workspace_ref = workspace
        self.align_workspace_pointer = workspace.as_pointer()
        self.align_workspace_name = workspace.name

    def is_target_window(self, window: Any) -> bool:
        """주어진 창이 현재 세션이 소유한 메인 창인지 확인합니다."""
        if window is None or not self.target_window_pointer:
            return False
        try:
            return window.as_pointer() == self.target_window_pointer
        except ReferenceError:
            return False

    def is_align_workspace(self, workspace: Any) -> bool:
        """주어진 작업공간이 현재 세션의 COLMAP Align 공간인지 확인합니다."""
        if workspace is None or not self.align_workspace_pointer:
            return False
        try:
            return workspace.as_pointer() == self.align_workspace_pointer
        except ReferenceError:
            return False

    def clear(self) -> None:
        """파일 교체나 종료 뒤 재사용하면 안 되는 세션 참조를 초기화합니다."""
        self.generation += 1
        self.phase = SESSION_INACTIVE
        self.session_active = False
        self.target_window_ref = None
        self.target_window_pointer = 0
        self.align_workspace_ref = None
        self.align_workspace_pointer = 0
        self.align_workspace_name = ""
        self.source_workspace_ref = None
        self.source_workspace_pointer = 0
        self.source_workspace_name = ""
        self.recovery_pending = False
        self.exit_requested = False
        self.input_guard_active = False
        self.input_guard_window_pointer = 0
        self.message = ""


@dataclass
class ViewportRegistryState:
    """하나의 메인 창에 배치한 A/P 뷰포트의 식별자를 보관합니다."""

    workspace_name: str = ""
    main_window_pointer: int = 0
    reference_area_pointer: int = 0
    moving_area_pointer: int = 0
    reference_region_pointer: int = 0
    moving_region_pointer: int = 0

    def clear(self) -> None:
        """만료된 메인 창 뷰포트 식별자를 모두 제거합니다."""
        self.workspace_name = ""
        self.main_window_pointer = 0
        self.reference_area_pointer = 0
        self.moving_area_pointer = 0
        self.reference_region_pointer = 0
        self.moving_region_pointer = 0

    def is_main_complete(self) -> bool:
        """메인 창 A/P 뷰포트와 윈도우 영역이 모두 등록됐는지 확인합니다."""
        return bool(
            self.workspace_name
            and self.main_window_pointer
            and self.reference_area_pointer
            and self.moving_area_pointer
            and self.reference_region_pointer
            and self.moving_region_pointer
        )

    def is_complete(self) -> bool:
        """이전 호출부 호환을 위해 메인 창 등록 완성 여부를 반환합니다."""
        return self.is_main_complete()


@dataclass
class ProjectRestoreRuntimeState:
    """대형 COLMAP 배열 없이 자동 복원 진행·오류·재연결 후보만 보관합니다."""

    phase: str = PROJECT_RESTORE_PHASE_IDLE
    generation: int = 0
    project_uuid: str = ""
    snapshot: Any = None
    current_model_index: int = 0
    issues: list[Any] = field(default_factory=list)
    last_error: str = ""
    last_message: str = ""
    relink_paths: dict[str, str] = field(default_factory=dict)

    def clear(self, *, keep_project_uuid: bool = False) -> None:
        """파일 교체·등록 해제 뒤 stale 복원 입력을 비우고 유휴 상태로 되돌립니다."""
        self.generation += 1
        self.phase = PROJECT_RESTORE_PHASE_IDLE
        if not keep_project_uuid:
            self.project_uuid = ""
        self.snapshot = None
        self.current_model_index = 0
        self.issues.clear()
        self.last_error = ""
        self.last_message = ""
        self.relink_paths.clear()


@dataclass
class RuntimeState:
    """GPU, 모달, 모델 카탈로그와 UI 미러 상태를 한 곳에서 관리합니다."""

    viewport_registry: ViewportRegistryState = field(default_factory=ViewportRegistryState)
    model_catalog: ModelCatalogState = field(default_factory=ModelCatalogState)
    model_repository: ColmapModelRepository = field(default_factory=ColmapModelRepository)
    marker_selection: MarkerSelectionState = field(default_factory=MarkerSelectionState)
    marker_alignment: MarkerAlignmentPreviewState = field(
        default_factory=MarkerAlignmentPreviewState
    )
    layer_transform: LayerTransformRuntimeState = field(
        default_factory=LayerTransformRuntimeState
    )
    icp_alignment: IcpAlignmentRuntimeState = field(default_factory=IcpAlignmentRuntimeState)
    merge_export: MergeExportRuntimeState = field(default_factory=MergeExportRuntimeState)
    obj_export: ObjExportRuntimeState = field(default_factory=ObjExportRuntimeState)
    project_restore: ProjectRestoreRuntimeState = field(
        default_factory=ProjectRestoreRuntimeState
    )
    project_persistence_dirty: bool = False
    point_clouds: dict[str, Any] = field(default_factory=dict)
    gpu_batches: dict[str, Any] = field(default_factory=dict)
    draw_handlers: list[tuple[str, Any]] = field(default_factory=list)
    modal_active: bool = False
    marker_command: str = MARKER_COMMAND_NONE
    ui_list_syncing: bool = False
    last_data_layer_click_model_id: str | None = None
    last_data_layer_click_time: float = 0.0
    role_view_states: dict[int, dict[str, Any]] = field(default_factory=dict)
    workspace_setup_task: Any = None
    workspace_setup_error: str = ""
    main_window_ref: Any = None
    undo_journal: UndoJournal = field(default_factory=UndoJournal)
    session: SessionState = field(default_factory=SessionState)
    session_exit_task: Any = None
    session_guard_timer: Any = None
    marker_modal_timer: Any = None
    import_job: Any = None
    import_timer: Any = None
    icp_timer: Any = None
    merge_export_timer: Any = None
    obj_export_timer: Any = None
    project_restore_timer: Any = None

    @property
    def session_active(self) -> bool:
        """세션 활성 상태를 SessionState에 위임해 반환합니다."""
        return self.session.session_active

    @session_active.setter
    def session_active(self, value: bool) -> None:
        """이전 호출부의 활성 플래그 변경을 SessionState에 반영합니다."""
        self.session.session_active = bool(value)

    def clear_data_layer_click(self) -> None:
        """N 패널 레이어 더블클릭 판정에 사용한 직전 클릭을 비웁니다."""
        self.last_data_layer_click_model_id = None
        self.last_data_layer_click_time = 0.0

    def request_marker_command(self, command: str) -> bool:
        """N 패널 명령을 한 번만 저장하되 취소를 항상 최우선으로 둡니다."""
        valid_commands = {MARKER_COMMAND_CONFIRM, MARKER_COMMAND_CANCEL}
        if command not in valid_commands:
            raise ValueError(f"지원하지 않는 마커 명령입니다: {command}")
        if command == MARKER_COMMAND_CANCEL:
            if self.marker_command == MARKER_COMMAND_CANCEL:
                return False
            self.marker_command = MARKER_COMMAND_CANCEL
            return True
        if self.marker_command != MARKER_COMMAND_NONE:
            return False
        self.marker_command = MARKER_COMMAND_CONFIRM
        return True

    def take_marker_command(self) -> str:
        """대기 중인 마커 명령을 한 번 반환한 뒤 즉시 비웁니다."""
        command = self.marker_command
        self.marker_command = MARKER_COMMAND_NONE
        return command

    def clear_marker_command(self) -> None:
        """처리한 마커 명령을 없는 상태로 초기화합니다."""
        self.marker_command = MARKER_COMMAND_NONE

    @property
    def import_in_progress(self) -> bool:
        """단일 COLMAP 가져오기 작업이 현재 진행 중인지 반환합니다."""
        return self.import_job is not None

    def clear_import_job(self) -> None:
        """완료·실패·취소 뒤 가져오기 작업과 타이머 참조를 끊습니다."""
        self.import_job = None
        self.import_timer = None

    def clear_icp_timer(self) -> None:
        """등록 해제와 작업 완료 뒤 UI가 소유한 ICP poll timer 참조를 끊습니다."""
        self.icp_timer = None

    def clear_merge_export_timer(self) -> None:
        """등록 해제와 작업 완료 뒤 구조 통합 poll timer 참조를 끊습니다."""
        self.merge_export_timer = None

    def clear_obj_export_timer(self) -> None:
        """등록 해제와 작업 완료 뒤 OBJ Export timer 참조를 끊습니다."""
        self.obj_export_timer = None

    def clear_transient(self, keep_markers: bool = False) -> None:
        """파일 교체에 취약한 GPU·면 피킹·창·Undo·카탈로그 참조를 안전하게 비웁니다."""
        from .alignment.icp_runtime import invalidate_icp
        from .viewport.picking import clear_mesh_surface_pick_cache

        invalidate_icp(self, "세션 상태를 초기화했습니다.")
        self.layer_transform.clear(message="세션 상태를 초기화했습니다.")
        if self.icp_timer is not None:
            from .operators.marker_alignment import stop_icp_poll_timer_for_lifecycle

            stop_icp_poll_timer_for_lifecycle()
        if self.merge_export.job is not None or self.merge_export_timer is not None:
            from .operators.export_model import invalidate_merge_export_for_lifecycle

            invalidate_merge_export_for_lifecycle("세션 상태를 초기화했습니다.")
        else:
            self.merge_export.clear()
        if self.obj_export.job is not None or self.obj_export_timer is not None:
            from .operators.export_obj import invalidate_obj_export_for_lifecycle

            invalidate_obj_export_for_lifecycle("세션 상태를 초기화했습니다.")
        else:
            self.obj_export.clear()
        self.viewport_registry.clear()
        self.model_catalog.clear()
        self.model_repository.clear()
        self.marker_alignment.clear()
        self.point_clouds.clear()
        clear_mesh_surface_pick_cache()
        self.gpu_batches.clear()
        self.draw_handlers.clear()
        self.modal_active = False
        self.clear_marker_command()
        self.ui_list_syncing = False
        self.clear_data_layer_click()
        self.role_view_states.clear()
        self.workspace_setup_task = None
        self.workspace_setup_error = ""
        self.main_window_ref = None
        self.undo_journal.clear()
        self.session_exit_task = None
        self.session_guard_timer = None
        self.marker_modal_timer = None
        self.clear_import_job()
        self.clear_icp_timer()
        self.clear_merge_export_timer()
        self.clear_obj_export_timer()
        self.project_persistence_dirty = False
        self.project_restore.clear()
        self.project_restore_timer = None
        self.session.clear()
        if not keep_markers:
            self.marker_selection.clear()


RUNTIME = RuntimeState()
