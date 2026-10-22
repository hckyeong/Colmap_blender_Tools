"""Data Layers의 선택, 그룹 이동, 표시 토글과 런타임 삭제를 처리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import bpy
from bpy.props import BoolProperty, StringProperty

from ..alignment.types import AlignmentProvenanceRecord
from ..axis_settings import axis_edit_active_block_reason
from ..display_axes import create_model_display_transform
from ..constants import (
    GEOMETRY_KIND_TRIANGLE_MESH,
    MODEL_ACTIVATION_SOURCE_MANUAL,
    MODEL_COMMAND_TOGGLE_LAYER_GROUP,
    MODEL_STATUS_REGISTERED,
)
from ..logging_utils import get_logger
from ..mesh_io.embedded_obj_store import (
    find_embedded_obj_mesh,
    remove_embedded_obj_mesh,
    retire_embedded_obj_mesh,
    validate_embedded_obj_mesh,
)
from ..project_persistence.runtime import (
    clear_project_state_after_last_model,
    notify_project_state_changed,
    restore_input_block_reason,
    sync_project_state_if_dirty,
)
from ..model_repository import model_point_count
from ..marker_capture import marker_capture_change_block_reason
from ..properties import sync_marker_pair_list
from ..session import is_session_active_for_window, session_align_workspace
from ..status import set_workspace_status_text
from ..state import RUNTIME
from ..viewport.picking import discard_mesh_surface_pick_cache_for_model
from ..viewport.point_data import surface_model_data_for_id
from ..viewport.renderer import discard_gpu_batch_for_model, request_redraw
from .marker_alignment import is_marker_preview_active
from .layer_transform import layer_transform_active_block_reason


_LOGGER = get_logger("operators.model_group")


@dataclass(frozen=True)
class ModelDeletionResult:
    """런타임 모델 삭제 뒤 사용자 보고와 테스트에 필요한 결과입니다."""

    model_id: str
    display_name: str
    point_count: int
    removed_marker_pair_count: int


def _report_status(operator: bpy.types.Operator, level: set[str], message: str) -> None:
    """Data Layers 결과를 Blender 상태 표시줄과 오퍼레이터 보고에 함께 전달합니다."""
    if not message:
        return
    set_workspace_status_text(session_align_workspace(), message)
    operator.report(level, message)


def _pair_references_model(pair, model_id: str) -> bool:
    """확정 마커 쌍의 어느 한쪽이라도 지정 모델을 참조하는지 반환합니다."""
    return bool(
        pair.reference.model_id == model_id
        or pair.moving.model_id == model_id
    )


def _gpu_batch_belongs_to_model(cache_key, model_id: str) -> bool:
    """GPU 캐시 키가 지정 모델의 배치인지 형식 변경에 안전하게 판정합니다."""
    return bool(
        isinstance(cache_key, tuple)
        and cache_key
        and str(cache_key[0]) == model_id
    )


def _marker_preview_block_reason() -> str | None:
    """marker preview 중 Data Layers 변경을 차단할 사유를 반환합니다."""
    if is_marker_preview_active():
        return "마커 정렬 미리보기를 취소한 뒤 데이터 레이어를 변경하십시오."
    return None


def _merge_export_block_reason() -> str | None:
    """내보내기 작업이 입력 snapshot을 사용하는 동안 변경 차단 사유를 반환합니다."""

    merge_state = getattr(RUNTIME, "merge_export", None)
    if bool(getattr(merge_state, "is_running", False)):
        return "구조 통합 내보내기 중에는 데이터 레이어를 변경할 수 없습니다. 작업을 취소하거나 완료를 기다리십시오."
    obj_state = getattr(RUNTIME, "obj_export", None)
    if bool(getattr(obj_state, "is_running", False)):
        return "OBJ 내보내기 중에는 데이터 레이어를 변경할 수 없습니다. 작업을 취소하거나 완료를 기다리십시오."
    return None


def _owned_embedded_assets_for_model(model_id: str) -> tuple[str, ...]:
    """삭제할 레이어의 주 OBJ와 연결 OBJ가 소유한 내장 UUID를 미리 검증합니다."""

    repository = RUNTIME.model_repository
    main = repository.models.get(model_id)
    companion = repository.companion_mesh_for_model_id(model_id)
    candidates = (
        (main, getattr(main, "geometry_kind", "") == GEOMETRY_KIND_TRIANGLE_MESH),
        (companion, companion is not None),
    )
    asset_uuids: list[str] = []
    for model_data, embedded_required in candidates:
        if model_data is None:
            continue
        asset_uuid = str(getattr(model_data, "embedded_asset_uuid", "")).strip()
        if not asset_uuid:
            if embedded_required:
                raise RuntimeError("삭제할 OBJ 레이어의 내장 Mesh UUID가 누락되었습니다.")
            continue
        mesh = find_embedded_obj_mesh(asset_uuid)
        validate_embedded_obj_mesh(
            mesh,
            expected_asset_uuid=asset_uuid,
            expected_model_id=model_id,
            expected_revision=int(getattr(model_data, "geometry_revision", 0)),
        )
        asset_uuids.append(asset_uuid)
    if len(set(asset_uuids)) != len(asset_uuids):
        raise RuntimeError("주 OBJ와 연결 OBJ가 같은 내장 Mesh UUID를 공유합니다.")
    return tuple(asset_uuids)


def model_deletion_block_reason(model_id: str | None = None) -> str | None:
    """선택 모델 삭제를 시작할 수 없는 정확한 이유를 반환합니다."""
    axes_reason = axis_edit_active_block_reason(RUNTIME)
    if axes_reason is not None:
        return axes_reason
    catalog = RUNTIME.model_catalog
    target_model_id = str(model_id or catalog.selected_model_id or "").strip()
    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        return restore_reason
    preview_reason = _marker_preview_block_reason()
    if preview_reason is not None:
        return preview_reason
    export_reason = _merge_export_block_reason()
    if export_reason is not None:
        return export_reason
    transform_reason = layer_transform_active_block_reason()
    if transform_reason is not None:
        return transform_reason
    if RUNTIME.import_in_progress:
        return "COLMAP 가져오기가 끝난 뒤 데이터 레이어를 삭제하십시오."
    if RUNTIME.modal_active or RUNTIME.marker_selection.has_draft:
        return "열린 마커 입력을 확정하거나 취소한 뒤 데이터 레이어를 삭제하십시오."
    if RUNTIME.undo_journal.scope_active:
        return "진행 중인 세션 편집을 마친 뒤 데이터 레이어를 삭제하십시오."
    if catalog.has_preview():
        return "정합 미리보기를 취소한 뒤 데이터 레이어를 삭제하십시오."
    if not target_model_id:
        return "삭제할 데이터 레이어를 먼저 선택하십시오."
    if target_model_id != catalog.selected_model_id:
        return "현재 선택된 데이터 레이어만 삭제할 수 있습니다."
    if target_model_id not in catalog.models:
        return "삭제할 데이터 레이어를 찾을 수 없습니다."
    if target_model_id not in RUNTIME.model_repository.models:
        return "삭제할 COLMAP 모델의 메모리 데이터를 찾을 수 없습니다."
    return None


def delete_model_from_runtime(model_id: str) -> ModelDeletionResult:
    """삭제 시 원본·피킹 자원을 해제하고 실패하면 준비 입력까지 원상 복원합니다."""
    normalized_id = str(model_id).strip()
    reason = model_deletion_block_reason(normalized_id)
    if reason is not None:
        raise RuntimeError(reason)

    catalog = RUNTIME.model_catalog
    repository = RUNTIME.model_repository
    selection = RUNTIME.marker_selection
    record = catalog.models[normalized_id]
    model = repository.models[normalized_id]
    display_transform = repository.display_transform_for_model_id(normalized_id)
    alignment_transform = repository.alignment_transform_for_model_id(normalized_id)
    source_metadata = repository.source_metadata.get(normalized_id)
    models_snapshot = dict(repository.models)
    source_index_snapshot = dict(repository.source_path_index)
    alignment_inputs_snapshot = dict(repository.alignment_inputs)
    # 연결 메시·개별 축·평가 캐시는 삭제 실패 시 원래 객체와 존재 여부까지 복원합니다.
    companion_snapshot = tuple(
        (mapping, normalized_id in mapping, mapping.get(normalized_id))
        for mapping in (
            repository.companion_meshes,
            repository.companion_sources,
            repository.companion_display_transforms,
            repository.icp_confidence_cache,
        )
    )
    point_count = model_point_count(model)

    catalog_snapshot = catalog.snapshot()
    marker_snapshot = selection.snapshot()
    point_cloud_present = normalized_id in RUNTIME.point_clouds
    point_cloud = RUNTIME.point_clouds.get(normalized_id)
    gpu_snapshot = {
        cache_key: batch
        for cache_key, batch in RUNTIME.gpu_batches.items()
        if _gpu_batch_belongs_to_model(cache_key, normalized_id)
    }
    embedded_asset_uuids = _owned_embedded_assets_for_model(normalized_id)

    try:
        removed_model = repository.remove_model(normalized_id)
        if removed_model is None:
            raise RuntimeError("삭제할 COLMAP 모델의 메모리 데이터를 찾을 수 없습니다.")
        if catalog.remove_model(normalized_id) is None:
            raise RuntimeError("삭제할 데이터 레이어를 찾을 수 없습니다.")
        removed_pair_count = selection.remove_pairs_for_model(normalized_id)
        RUNTIME.point_clouds.pop(normalized_id, None)
        discard_gpu_batch_for_model(normalized_id)
        if repository.models:
            notify_project_state_changed("모델 삭제 완료")
            sync_project_state_if_dirty(force=True)
        else:
            clear_project_state_after_last_model()
    except Exception:
        catalog.restore_snapshot(catalog_snapshot)
        selection.restore_snapshot(marker_snapshot)
        if normalized_id not in repository.models:
            imported_display = (
                create_model_display_transform(
                    source_metadata.axis_preset,
                    source_metadata.axis_forward,
                    source_metadata.axis_up,
                )
                if source_metadata is not None
                else display_transform
            )
            repository.add_model(model, imported_display, source_metadata)
            repository.set_display_transform(normalized_id, display_transform)
            repository.set_alignment_transform(normalized_id, alignment_transform)
        repository.models.clear()
        repository.models.update(models_snapshot)
        repository.source_path_index.clear()
        repository.source_path_index.update(source_index_snapshot)
        repository.alignment_inputs.clear()
        repository.alignment_inputs.update(alignment_inputs_snapshot)
        for mapping, present, value in companion_snapshot:
            if present:
                mapping[normalized_id] = value
            else:
                mapping.pop(normalized_id, None)
        if point_cloud_present:
            RUNTIME.point_clouds[normalized_id] = point_cloud
        else:
            RUNTIME.point_clouds.pop(normalized_id, None)
        discard_gpu_batch_for_model(normalized_id)
        RUNTIME.gpu_batches.update(gpu_snapshot)
        raise

    discard_mesh_surface_pick_cache_for_model(normalized_id)
    for asset_uuid in embedded_asset_uuids:
        try:
            remove_embedded_obj_mesh(asset_uuid, expected_model_id=normalized_id)
        except Exception as error:
            try:
                retire_embedded_obj_mesh(asset_uuid, expected_model_id=normalized_id)
            except Exception:
                _LOGGER.exception(
                    "event=embedded_obj_retire_failed model_id=%s asset_uuid=%s",
                    normalized_id,
                    asset_uuid,
                )
            _LOGGER.error(
                "event=embedded_obj_remove_failed model_id=%s asset_uuid=%s reason=%s",
                normalized_id,
                asset_uuid,
                error,
            )

    RUNTIME.undo_journal.clear()
    RUNTIME.clear_data_layer_click()
    _LOGGER.info(
        "event=model_deleted model_id=%s points=%d marker_pairs=%d embedded_assets=%d",
        normalized_id,
        point_count,
        removed_pair_count,
        len(embedded_asset_uuids),
    )
    return ModelDeletionResult(
        model_id=normalized_id,
        display_name=record.display_name,
        point_count=point_count,
        removed_marker_pair_count=removed_pair_count,
    )


def _double_click_interval_seconds(context) -> float:
    """Blender 사용자 입력 설정의 더블클릭 시간을 초 단위로 반환합니다."""
    preferences = getattr(context, "preferences", None)
    inputs = getattr(preferences, "inputs", None)
    interval_ms = getattr(inputs, "mouse_double_click_time", 350)
    return max(1.0, float(interval_ms)) / 1000.0


def _is_layer_double_click(context, event, model_id: str) -> bool:
    """공개 DOUBLE_CLICK 또는 같은 행의 설정 시간 내 연속 클릭을 판정합니다."""
    if getattr(event, "value", "") == "DOUBLE_CLICK":
        RUNTIME.clear_data_layer_click()
        return True
    current_time = monotonic()
    elapsed = current_time - RUNTIME.last_data_layer_click_time
    is_double = bool(
        RUNTIME.last_data_layer_click_model_id == model_id
        and 0.0 <= elapsed <= _double_click_interval_seconds(context)
    )
    if is_double:
        RUNTIME.clear_data_layer_click()
    else:
        RUNTIME.last_data_layer_click_model_id = model_id
        RUNTIME.last_data_layer_click_time = current_time
    return is_double


def _commit_group_change(
    operator: bpy.types.Operator,
    model_id: str,
    *,
    allow_demote: bool,
) -> set[str]:
    """축 편집 중 그룹 변경을 차단하고 A/P 전환을 하나의 세션 Undo로 확정합니다."""
    axes_reason = axis_edit_active_block_reason(RUNTIME)
    if axes_reason is not None:
        _report_status(operator, {"WARNING"}, axes_reason)
        return {"CANCELLED"}
    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        _report_status(operator, {"WARNING"}, restore_reason)
        return {"CANCELLED"}
    preview_reason = _marker_preview_block_reason()
    if preview_reason is not None:
        _report_status(operator, {"WARNING"}, preview_reason)
        return {"CANCELLED"}
    export_reason = _merge_export_block_reason()
    if export_reason is not None:
        _report_status(operator, {"WARNING"}, export_reason)
        return {"CANCELLED"}
    transform_reason = layer_transform_active_block_reason()
    if transform_reason is not None:
        _report_status(operator, {"WARNING"}, transform_reason)
        return {"CANCELLED"}
    catalog = RUNTIME.model_catalog
    selection = RUNTIME.marker_selection
    before = catalog.snapshot()
    if allow_demote:
        changed = catalog.transfer_model_group(
            model_id,
            source=MODEL_ACTIVATION_SOURCE_MANUAL,
            has_marker_draft=selection.has_draft,
            completed_pair_count=len(selection.completed_pairs),
        )
    else:
        changed = catalog.promote_model_to_active(
            model_id,
            source=MODEL_ACTIVATION_SOURCE_MANUAL,
            has_marker_draft=selection.has_draft,
            completed_pair_count=len(selection.completed_pairs),
        )
    if not changed:
        _report_status(operator, {"WARNING"}, catalog.message)
        request_redraw()
        return {"CANCELLED"}
    target_group = (
        "A"
        if catalog.models[model_id].registration_status == MODEL_STATUS_REGISTERED
        else "P"
    )
    if target_group == "A":
        # 수동 A 편입은 품질 판정이 아니라 사용자의 포함 결정임을 감사 기록에 남깁니다.
        catalog.set_alignment_provenance(
            model_id,
            AlignmentProvenanceRecord(
                model_id=model_id,
                method="MANUAL",
            ),
        )
    after = catalog.snapshot()
    action = f"{catalog.models[model_id].display_name} → {target_group} 그룹"
    if not RUNTIME.undo_journal.record_committed(
        before,
        after,
        action,
        MODEL_COMMAND_TOGGLE_LAYER_GROUP,
    ):
        catalog.restore_snapshot(before)
        message = "레이어 그룹 Undo 이력을 기록하지 못해 변경을 취소했습니다."
        catalog.message = message
        _report_status(operator, {"ERROR"}, message)
        request_redraw()
        return {"CANCELLED"}
    _report_status(operator, {"INFO"}, catalog.message)
    notify_project_state_changed("Data Layers 그룹 변경")
    request_redraw()
    return {"FINISHED"}


class COLMAPALIGN_OT_select_data_layer(bpy.types.Operator):
    """일반 단일 선택, Ctrl 개별 토글, Shift 범위 선택과 더블클릭 그룹 이동을 처리합니다."""

    bl_idname = "colmap_align.select_data_layer"
    bl_label = "데이터 레이어 선택"
    bl_description = "일반 클릭은 단일 선택, Ctrl 클릭은 개별 토글, Shift 클릭은 연속 범위를 선택합니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})
    extend_selection: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    range_selection: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션에서만 Data Layers 입력을 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not is_marker_preview_active()
            and _merge_export_block_reason() is None
            and layer_transform_active_block_reason() is None
            and axis_edit_active_block_reason(RUNTIME) is None
            and restore_input_block_reason() is None
        )

    def invoke(self, context, event):
        """Shift 범위·Ctrl 토글을 우선 판정한 뒤 단일 선택 또는 그룹 전환으로 라우팅합니다."""
        self.range_selection = bool(getattr(event, "shift", False))
        self.extend_selection = bool(
            getattr(event, "ctrl", False) and not self.range_selection
        )
        if self.range_selection or self.extend_selection:
            RUNTIME.clear_data_layer_click()
            return self.execute(context)
        if _is_layer_double_click(context, event, self.model_id):
            return _commit_group_change(self, self.model_id, allow_demote=True)
        return self.execute(context)

    def execute(self, _context):
        """선택 모드에 맞춰 단일·Ctrl 토글·Shift 범위를 적용하고 Undo에는 기록하지 않습니다."""
        axes_reason = axis_edit_active_block_reason(RUNTIME)
        if axes_reason is not None:
            _report_status(self, {"WARNING"}, axes_reason)
            return {"CANCELLED"}
        preview_reason = _marker_preview_block_reason()
        if preview_reason is not None:
            _report_status(self, {"WARNING"}, preview_reason)
            return {"CANCELLED"}
        transform_reason = layer_transform_active_block_reason()
        if transform_reason is not None:
            _report_status(self, {"WARNING"}, transform_reason)
            return {"CANCELLED"}
        selection = RUNTIME.marker_selection
        if self.range_selection:
            changed = RUNTIME.model_catalog.select_model_range(
                self.model_id,
                has_marker_draft=selection.has_draft,
                completed_pair_count=len(selection.completed_pairs),
            )
        elif self.extend_selection:
            changed = RUNTIME.model_catalog.toggle_alignment_selection(
                self.model_id,
                has_marker_draft=selection.has_draft,
                completed_pair_count=len(selection.completed_pairs),
            )
        else:
            changed = RUNTIME.model_catalog.select_model(
                self.model_id,
                has_marker_draft=selection.has_draft,
                completed_pair_count=len(selection.completed_pairs),
            )
        if not changed:
            _report_status(self, {"WARNING"}, RUNTIME.model_catalog.message)
            return {"CANCELLED"}
        _report_status(self, {"INFO"}, RUNTIME.model_catalog.message)
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_toggle_data_layer_group(bpy.types.Operator):
    """선택 이벤트와 독립적으로 A/P 그룹 전환 서비스를 노출합니다."""

    bl_idname = "colmap_align.toggle_data_layer_group"
    bl_label = "데이터 레이어 그룹 전환"
    bl_description = "데이터 레이어를 A 그룹과 P 그룹 사이에서 이동합니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션에서만 그룹 전환을 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not is_marker_preview_active()
            and _merge_export_block_reason() is None
            and layer_transform_active_block_reason() is None
            and axis_edit_active_block_reason(RUNTIME) is None
            and restore_input_block_reason() is None
        )

    def execute(self, _context):
        """명시적 호출도 더블클릭과 같은 양방향 전환 경계를 사용합니다."""
        RUNTIME.clear_data_layer_click()
        return _commit_group_change(self, self.model_id, allow_demote=True)


class COLMAPALIGN_OT_activate_data_layer(bpy.types.Operator):
    """후속 정합 승인에서 P 레이어만 A 그룹으로 편입합니다."""

    bl_idname = "colmap_align.activate_data_layer"
    bl_label = "데이터 레이어 활성화"
    bl_description = "후속 정합 승인용으로 P 레이어를 A 그룹에 편입합니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션에서만 정합 승인 경계를 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not is_marker_preview_active()
            and _merge_export_block_reason() is None
            and layer_transform_active_block_reason() is None
            and axis_edit_active_block_reason(RUNTIME) is None
            and restore_input_block_reason() is None
        )

    def execute(self, _context):
        """정합 엔진이 사용할 P→A 단방향 전환만 수행합니다."""
        RUNTIME.clear_data_layer_click()
        return _commit_group_change(self, self.model_id, allow_demote=False)


class COLMAPALIGN_OT_toggle_data_layer_visibility(bpy.types.Operator):
    """레이어 하나의 GPU 표시 여부를 다른 상태와 독립적으로 반전합니다."""

    bl_idname = "colmap_align.toggle_data_layer_visibility"
    bl_label = "데이터 레이어 표시 전환"
    bl_description = "이 레이어의 뷰포트 표시 여부만 전환합니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def description(cls, _context, properties):
        """전체 표시 아이콘의 툴팁에 해당 레이어 이름을 표시합니다."""
        model_id = getattr(properties, "model_id", "")
        record = RUNTIME.model_catalog.models.get(model_id)
        name = record.display_name if record is not None else model_id
        return f"{name}: 데이터셋 전체 표시를 전환합니다"

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션에서만 레이어 표시 토글을 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not is_marker_preview_active()
            and _merge_export_block_reason() is None
            and layer_transform_active_block_reason() is None
            and restore_input_block_reason() is None
        )

    def execute(self, _context):
        """입력 대상 잠금을 검사한 뒤 전체 표시 상태를 반전합니다."""
        capture_reason = marker_capture_change_block_reason(RUNTIME, self.model_id)
        if capture_reason is not None:
            _report_status(self, {"WARNING"}, capture_reason)
            return {"CANCELLED"}
        preview_reason = _marker_preview_block_reason()
        if preview_reason is not None:
            _report_status(self, {"WARNING"}, preview_reason)
            return {"CANCELLED"}
        transform_reason = layer_transform_active_block_reason()
        if transform_reason is not None:
            _report_status(self, {"WARNING"}, transform_reason)
            return {"CANCELLED"}
        RUNTIME.clear_data_layer_click()
        if not RUNTIME.model_catalog.toggle_model_visibility(self.model_id):
            _report_status(self, {"WARNING"}, RUNTIME.model_catalog.message)
            return {"CANCELLED"}
        _report_status(self, {"INFO"}, RUNTIME.model_catalog.message)
        notify_project_state_changed("Data Layers 표시 상태 변경")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_toggle_data_layer_geometry(bpy.types.Operator):
    """데이터셋에서 점군 또는 연결·독립 메시 하나를 표시 형상으로 선택합니다."""

    bl_idname = "colmap_align.toggle_data_layer_geometry"
    bl_label = "데이터셋 형상 선택"
    bl_description = "점군 또는 메시 하나를 선택합니다. 숨기기는 눈 버튼을 사용합니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})
    geometry_kind: StringProperty(options={"HIDDEN"})

    @classmethod
    def description(cls, _context, properties):
        """형상 표시 아이콘에 대상 레이어 이름과 점군·메시 구분을 표시합니다."""
        model_id = getattr(properties, "model_id", "")
        record = RUNTIME.model_catalog.models.get(model_id)
        name = record.display_name if record is not None else model_id
        geometry = "점군" if getattr(properties, "geometry_kind", "") == "COLMAP_POINT_CLOUD" else "메시"
        return f"{name}: {geometry} 표시를 선택합니다"

    @classmethod
    def poll(cls, context):
        """가져오기·복원 이외에는 정렬 미리보기 중에도 형상 표시만 변경할 수 있습니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and restore_input_block_reason() is None
        )

    def execute(self, _context):
        """입력 대상 잠금을 검사하고 클릭한 행의 형상 하나를 선택합니다."""
        capture_reason = marker_capture_change_block_reason(RUNTIME, self.model_id)
        if capture_reason is not None:
            _report_status(self, {"WARNING"}, capture_reason)
            return {"CANCELLED"}
        RUNTIME.clear_data_layer_click()
        catalog = RUNTIME.model_catalog
        record = catalog.models.get(self.model_id)
        if record is None:
            _report_status(self, {"WARNING"}, "표시를 변경할 데이터셋을 찾을 수 없습니다.")
            return {"CANCELLED"}
        if self.geometry_kind == "COLMAP_POINT_CLOUD" and record.geometry_kind == "COLMAP_POINT_CLOUD":
            values = {"show_points": True, "show_surface": False}
        elif self.geometry_kind == "TRIANGLE_MESH" and surface_model_data_for_id(self.model_id) is not None:
            values = {"show_points": False, "show_surface": True}
        else:
            _report_status(self, {"WARNING"}, "표시를 변경할 연결 표면을 찾을 수 없습니다.")
            return {"CANCELLED"}
        if not catalog.set_geometry_visibility(self.model_id, **values):
            _report_status(self, {"WARNING"}, catalog.message)
            return {"CANCELLED"}
        _report_status(self, {"INFO"}, catalog.message)
        notify_project_state_changed("데이터셋 점군·표면 표시 변경")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_delete_selected_model(bpy.types.Operator):
    """Delete 키로 선택한 COLMAP 데이터 레이어를 메모리에서 제거합니다."""

    bl_idname = "colmap_align.delete_selected_model"
    bl_label = "선택한 COLMAP 데이터 레이어 삭제"
    bl_description = "선택한 COLMAP 모델을 메모리에서 제거하며 원본 폴더는 삭제하지 않습니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션에서 가져오기가 없을 때만 삭제 요청을 받습니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not is_marker_preview_active()
            and _merge_export_block_reason() is None
            and layer_transform_active_block_reason() is None
            and axis_edit_active_block_reason(RUNTIME) is None
            and restore_input_block_reason() is None
        )

    def invoke(self, context, event):
        """선택 모델과 연관 마커 수를 고정한 뒤 파괴적 메모리 해제를 확인합니다."""
        reason = model_deletion_block_reason()
        if reason is not None:
            _report_status(self, {"WARNING"}, reason)
            return {"CANCELLED"}
        self.model_id = str(RUNTIME.model_catalog.selected_model_id or "")
        record = RUNTIME.model_catalog.models[self.model_id]
        pair_count = sum(
            1
            for pair in RUNTIME.marker_selection.completed_pairs
            if _pair_references_model(pair, self.model_id)
        )
        marker_message = (
            f" 연결된 마커 쌍 {pair_count}개도 함께 삭제됩니다."
            if pair_count
            else ""
        )
        message = (
            f"'{record.display_name}' 데이터 레이어를 메모리에서 삭제하시겠습니까?"
            f"{marker_message} 원본 COLMAP 폴더는 삭제되지 않습니다."
        )
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="COLMAP 데이터 레이어 삭제",
            message=message,
            confirm_text="삭제",
            icon="WARNING",
        )

    def execute(self, context):
        """확인 시점의 선택 모델만 원자 삭제하고 UI 미러와 뷰포트를 갱신합니다."""
        target_model_id = str(self.model_id or RUNTIME.model_catalog.selected_model_id or "")
        try:
            result = delete_model_from_runtime(target_model_id)
        except Exception as error:
            _LOGGER.error(
                "event=model_delete_failed model_id=%s reason=%s",
                target_model_id or "<none>",
                error,
            )
            _report_status(self, {"ERROR"}, str(error))
            request_redraw()
            return {"CANCELLED"}

        sync_marker_pair_list(context.window_manager)
        message = (
            f"{result.display_name} 데이터 레이어와 점 {result.point_count:,}개를 "
            "메모리에서 삭제했습니다. 원본 COLMAP 폴더는 유지됩니다."
        )
        RUNTIME.model_catalog.message = message
        _report_status(self, {"INFO"}, message)
        request_redraw()
        return {"FINISHED"}


CLASSES = (
    COLMAPALIGN_OT_select_data_layer,
    COLMAPALIGN_OT_toggle_data_layer_group,
    COLMAPALIGN_OT_activate_data_layer,
    COLMAPALIGN_OT_toggle_data_layer_visibility,
    COLMAPALIGN_OT_toggle_data_layer_geometry,
    COLMAPALIGN_OT_delete_selected_model,
)
