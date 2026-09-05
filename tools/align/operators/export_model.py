"""COLMAP 구조 통합 내보내기 UI·worker 수명주기 경계를 처리합니다."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
import numpy as np

from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MERGE_EXPORT_ALGORITHM_VERSION,
    MERGE_EXPORT_PHASE_CANCEL_PENDING,
    MERGE_EXPORT_PHASE_CANCELLED,
    MERGE_EXPORT_PHASE_COMPLETED,
    MERGE_EXPORT_PHASE_EXPORT_RUNNING,
    MERGE_EXPORT_PHASE_FAILED,
    MERGE_EXPORT_PHASE_IDLE,
    MERGE_EXPORT_PHASE_PREFLIGHT_READY,
    MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING,
    MERGE_EXPORT_PHASE_WAITING_FOR_FINALIZE,
    MERGE_EXPORT_PROTOCOL_VERSION,
)
from ..project_persistence.runtime import restore_input_block_reason
from ..session import is_session_active_for_window, session_align_workspace
from ..state import RUNTIME
from ..status import set_workspace_status_text
from ..viewport.renderer import request_redraw


_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_MERGE_EXPORT_DIRECTORY_NAME = "colmap_merged"


def _merge_export_state() -> Any:
    """Runtime이 소유한 구조 통합 상태를 검증 없이 반환합니다."""

    return RUNTIME.merge_export


def merge_export_phase() -> str:
    """UI가 읽을 현재 구조 통합 phase를 문자열로 반환합니다."""

    return str(getattr(_merge_export_state(), "phase", MERGE_EXPORT_PHASE_IDLE))


def is_merge_export_running() -> bool:
    """worker 입력 snapshot을 보호해야 하는 실행 단계를 반환합니다."""

    return bool(getattr(_merge_export_state(), "is_running", False))


def is_merge_export_input_locked() -> bool:
    """Data Layers·Undo·마커 입력을 잠가야 하는지 반환합니다."""

    return is_merge_export_running()


def _report_status(operator: bpy.types.Operator, level: set[str], message: str) -> None:
    """오퍼레이터 보고와 작업공간 상태줄에 같은 문구를 전달합니다."""

    operator.report(level, message)
    set_workspace_status_text(session_align_workspace(), message)


def format_merge_export_error(message: str) -> tuple[str, str, str]:
    """내보내기 오류를 원인·영향·사용자 해결 방법으로 정리합니다."""

    normalized = " ".join(str(message).split())
    lowered = normalized.lower()
    if "point2d" in lowered and ("uint32" in lowered or "범위" in normalized):
        return (
            "원인: 한 output 이미지에 연결한 source 특징점 수가 POINT2D_IDX 범위를 넘습니다.",
            "영향: source별 offset을 uint32 인덱스로 안전하게 기록할 수 없습니다.",
            "해결: 겹치는 이미지가 포함된 source 수를 줄여 별도 데이터셋으로 내보내십시오.",
        )
    if "output" in lowered and ("exist" in lowered or "존재" in normalized):
        return (
            "원인: 선택한 출력 폴더가 이미 존재합니다.",
            "영향: 명시적인 사용자 승인 없이 기존 결과를 교체하지 않았습니다.",
            "해결: Export를 다시 누르고 표시되는 덮어쓰기 확인 메시지에서 승인하십시오.",
        )
    return (
        f"원인: {normalized or '구조 통합 내보내기에서 확인되지 않은 오류가 발생했습니다.'}",
        "영향: 불완전한 COLMAP 결과는 최종 출력 폴더로 확정하지 않았습니다.",
        "해결: 출력 경로와 포함 모델의 COLMAP 입력을 확인한 뒤 사전 검사를 다시 실행하십시오.",
    )


def merge_export_status_entries() -> tuple[str, ...]:
    """패널과 GUI 테스트가 공유할 짧은 구조 통합 상태 문구를 반환합니다."""

    state = _merge_export_state()
    progress = getattr(state, "progress", None)
    entries = [f"상태 · {merge_export_phase()}"]
    stage = str(getattr(progress, "stage", "")).strip()
    if stage:
        entries.append(f"단계 · {stage}")
    message = str(getattr(progress, "message", "")).strip()
    if message:
        entries.append(message)
    snapshot = getattr(state, "snapshot", None)
    excluded_model_ids = tuple(getattr(snapshot, "excluded_model_ids", ())) if snapshot is not None else ()
    if excluded_model_ids:
        entries.append(
            f"폴리곤 메시 {len(excluded_model_ids)}개는 COLMAP Export에서 제외되며 OBJ Export로 내보낼 수 있습니다. "
            f"(ID: {', '.join(excluded_model_ids)})"
        )
    error = str(getattr(state, "last_error", "")).strip()
    if error:
        issue = getattr(state, "last_issue", None)
        if issue is not None:
            entries.extend(
                (
                    f"원인: {issue.cause}",
                    f"영향: {issue.impact}",
                    f"해결: {issue.remedy}",
                )
            )
        else:
            entries.extend(format_merge_export_error(error))
        diagnostic_path = str(getattr(state, "last_diagnostic_path", "")).strip()
        if diagnostic_path:
            entries.append(f"진단 파일 · {diagnostic_path}")
    return tuple(entries)


def _merge_export_api() -> tuple[Any, Any, Any, Any, Any]:
    """UI가 사용할 구조 통합 backend와 작은 공개 타입만 지연 로드합니다."""

    from ..merge_export.backend import MergeExportBackend
    from ..merge_export.types import (
        MergeExportSnapshot,
        MergePhase,
        MergePolicy,
        MergeSourceModelSnapshot,
    )

    return (
        MergeExportBackend,
        MergeExportSnapshot,
        MergePhase,
        MergePolicy,
        MergeSourceModelSnapshot,
    )


def _ordered_included_models() -> tuple[Any, ...]:
    """A 그룹에서 삼각형 메시를 제외한 COLMAP 레이어를 결정적 순서로 반환합니다."""

    catalog = RUNTIME.model_catalog
    repository = RUNTIME.model_repository
    active_models = tuple(catalog.active_models())
    colmap_models = tuple(
        record
        for record in active_models
        if str(getattr(record, "geometry_kind", "")).strip().upper()
        in {"", GEOMETRY_KIND_COLMAP_POINT_CLOUD}
        and (
            record.model_id not in repository.models
            or repository.geometry_kind_for_model_id(record.model_id) == GEOMETRY_KIND_COLMAP_POINT_CLOUD
        )
    )
    if not colmap_models:
        raise RuntimeError("구조 통합 내보내기에 포함할 COLMAP 레이어가 없습니다. OBJ 메시만 선택되었습니다.")
    anchor = next((record for record in colmap_models if record.is_anchor), colmap_models[0])
    return tuple(
        sorted(
            colmap_models,
            key=lambda record: (
                0 if record.model_id == anchor.model_id else 1,
                int(record.load_order),
                str(record.model_id),
            ),
        )
    )


def _excluded_polygon_model_ids() -> tuple[str, ...]:
    """A 그룹에서 Merge Export 대상에서 제외한 삼각형 메시 ID를 반환합니다."""

    repository = RUNTIME.model_repository
    excluded = []
    for record in RUNTIME.model_catalog.active_models():
        geometry_kind = str(getattr(record, "geometry_kind", "")).strip().upper()
        if geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH or (
            record.model_id in repository.models
            and repository.geometry_kind_for_model_id(record.model_id) == GEOMETRY_KIND_TRIANGLE_MESH
        ):
            excluded.append(str(record.model_id))
    return tuple(excluded)


def _provenance_mapping(record: Any) -> dict[str, Any] | None:
    """누락 provenance는 fatal로 바꾸지 않고 worker 감사 경고용 None으로 전달합니다."""

    provenance = getattr(record, "alignment_provenance", None)
    if provenance is None:
        return None
    to_mapping = getattr(provenance, "to_mapping", None)
    if not callable(to_mapping):
        raise RuntimeError("Data Layers 정렬 provenance 형식이 올바르지 않습니다.")
    mapping = to_mapping()
    if not isinstance(mapping, dict):
        raise RuntimeError("Data Layers 정렬 provenance JSON 형식이 올바르지 않습니다.")
    return mapping


def _runtime_fingerprint(
    source_models: tuple[Any, ...],
    excluded_model_ids: tuple[str, ...] = (),
) -> str:
    """포함 COLMAP과 제외 메시를 함께 현재 상태의 결정적 지문으로 만듭니다."""

    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"colmap-align-merge-runtime-v2\0")
    for source in source_models:
        payload = {
            "model_id": source.model_id,
            "display_name": source.display_name,
            "dataset_root": str(source.dataset_root),
            "source_path": str(source.source_path),
            "source_format": source.source_format,
            "catalog_load_order": source.catalog_load_order,
            "geometry_revision": source.geometry_revision,
            "is_anchor": source.is_anchor,
            "display_fingerprint": source.display_fingerprint,
            "alignment_fingerprint": source.alignment_fingerprint,
            "expected_source_fingerprint": source.expected_source_fingerprint,
            "alignment_provenance": (
                None
                if source.alignment_provenance is None
                else dict(source.alignment_provenance)
            ),
        }
        digest.update(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            np.ascontiguousarray(source.display_matrix_float64, dtype=np.dtype("<f8")).tobytes()
        )
        digest.update(
            np.ascontiguousarray(source.alignment_matrix_float64, dtype=np.dtype("<f8")).tobytes()
        )
        digest.update(b"\0")
    for model_id in excluded_model_ids:
        digest.update(b"excluded-mesh\0")
        digest.update(str(model_id).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _fallback_dataset_root(source_path: Path) -> Path:
    """구형 fixture의 sparse 경로에서 표준 COLMAP Dataset Root를 결정합니다."""

    resolved = Path(source_path).expanduser().resolve(strict=False)
    if resolved.name.casefold() == "0" and resolved.parent.name.casefold() == "sparse":
        return resolved.parent.parent
    if resolved.name.casefold() == "sparse":
        return resolved.parent
    return resolved


def _build_merge_export_snapshot(
    output_root: Path,
    *,
    output_format: str = "TEXT",
    restore_import_axes: bool = True,
    overwrite_existing_output: bool = False,
) -> Any:
    """현재 A 그룹·확정 변환만 고정해 worker가 다시 읽을 작은 snapshot을 생성합니다."""

    (
        _backend_type,
        snapshot_type,
        _phase_type,
        policy_type,
        source_snapshot_type,
    ) = _merge_export_api()
    catalog = RUNTIME.model_catalog
    repository = RUNTIME.model_repository
    ordered_records = _ordered_included_models()
    anchor = next((record for record in ordered_records if record.is_anchor), ordered_records[0])
    excluded_model_ids = _excluded_polygon_model_ids()
    source_models: list[Any] = []
    for record in ordered_records:
        model = repository.models.get(record.model_id)
        if model is None:
            raise RuntimeError(f"포함 모델의 메모리 데이터를 찾을 수 없습니다: {record.display_name}")
        try:
            display = repository.display_transform_for_model_id(record.model_id)
            alignment = repository.alignment_transform_for_model_id(record.model_id)
        except Exception as error:
            raise RuntimeError(
                f"포함 모델의 확정 변환을 찾을 수 없습니다: {record.display_name}"
            ) from error
        source_metadata = repository.source_metadata.get(record.model_id)
        if source_metadata is None:
            raise RuntimeError(
                f"포함 모델의 Import source fingerprint를 찾을 수 없습니다: {record.display_name}"
            )
        dataset_root = Path(source_metadata.selected_root_absolute)
        source_models.append(
            source_snapshot_type(
                model_id=record.model_id,
                display_name=record.display_name,
                dataset_root=dataset_root,
                source_path=Path(model.source_path),
                source_format=str(model.source_format),
                catalog_load_order=int(record.load_order),
                geometry_revision=int(model.geometry_revision),
                is_anchor=bool(record.model_id == anchor.model_id),
                display_matrix_float64=display.matrix_float64,
                display_fingerprint=display.fingerprint,
                alignment_matrix_float64=alignment.matrix_float64,
                alignment_fingerprint=alignment.fingerprint,
                expected_source_fingerprint=source_metadata.source_fingerprint,
                alignment_provenance=_provenance_mapping(record),
            )
        )
    immutable_sources = tuple(source_models)
    return snapshot_type(
        job_id=uuid4().hex,
        protocol_version=MERGE_EXPORT_PROTOCOL_VERSION,
        algorithm_version=MERGE_EXPORT_ALGORITHM_VERSION,
        output_root=output_root,
        anchor_model_id=anchor.model_id,
        source_models=immutable_sources,
        runtime_fingerprint=_runtime_fingerprint(immutable_sources, excluded_model_ids),
        output_format=output_format,
        restore_import_axes=restore_import_axes,
        overwrite_existing_output=overwrite_existing_output,
        merge_policy=policy_type(),
        excluded_model_ids=excluded_model_ids,
    )


def _normalized_output_root(raw_path: str, *, allow_existing: bool = False) -> Path:
    """출력 경로를 정규화하고 승인 없는 덮어쓰기와 원본 내부 경로를 거부합니다."""

    path_text = str(raw_path).strip()
    if not path_text:
        raise ValueError("새 통합 출력 폴더를 지정하십시오.")
    output_root = Path(bpy.path.abspath(path_text)).expanduser().resolve(strict=False)
    if output_root.exists() and not allow_existing:
        raise ValueError("선택한 출력 폴더가 이미 존재합니다. 새 폴더 경로를 지정하십시오.")
    if output_root.exists() and (output_root.is_symlink() or not output_root.is_dir()):
        raise ValueError("덮어쓸 출력 경로는 심볼릭 링크가 아닌 폴더여야 합니다.")
    if output_root.parent == output_root or not output_root.parent.is_dir():
        raise ValueError("출력 폴더의 상위 폴더가 존재해야 합니다.")
    for record in RUNTIME.model_catalog.active_models():
        model = RUNTIME.model_repository.models.get(record.model_id)
        if model is None:
            continue
        source_metadata = RUNTIME.model_repository.source_metadata.get(record.model_id)
        source_root = (
            Path(source_metadata.selected_root_absolute)
            if source_metadata is not None
            else _fallback_dataset_root(Path(model.source_path))
        ).expanduser().resolve(strict=False)
        try:
            output_root.relative_to(source_root)
        except ValueError:
            continue
        raise ValueError("출력 폴더는 어떤 원본 COLMAP Dataset Root 내부에도 둘 수 없습니다.")
    return output_root


def _output_root_under_parent(parent_path: str) -> Path:
    """선택한 기존 부모 폴더 아래의 고정 COLMAP 데이터셋 경로를 반환합니다."""

    parent = Path(bpy.path.abspath(str(parent_path).strip())).expanduser().resolve(
        strict=False
    )
    if not parent.is_dir():
        raise ValueError("통합 출력의 부모 폴더가 존재해야 합니다.")
    return parent / _DEFAULT_MERGE_EXPORT_DIRECTORY_NAME


def _merge_export_browser_start_directory(context: Any) -> Path:
    """마지막 COLMAP 선택 경로를 내보내기 파일 탐색기의 시작 위치로 반환합니다."""

    settings = context.scene.colmap_align_settings
    candidate_paths = (
        str(settings.import_last_directory).strip(),
        str(settings.import_dataset_root).strip(),
        bpy.path.abspath("//"),
    )
    for raw_path in candidate_paths:
        if not raw_path:
            continue
        candidate = Path(bpy.path.abspath(raw_path)).expanduser().resolve(strict=False)
        if candidate.is_dir():
            return candidate
    return Path.cwd().resolve()


def merge_export_start_block_reason(*, require_output_path: bool) -> str | None:
    """사전 검사·내보내기 시작 전에 보여줄 단일 차단 사유를 반환합니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(RUNTIME)
    if axis_reason is not None:
        return axis_reason

    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        return restore_reason

    if is_merge_export_running():
        return "구조 통합 내보내기 작업이 이미 실행 중입니다. 취소하거나 완료를 기다리십시오."
    if bool(getattr(RUNTIME.obj_export, "is_running", False)):
        return "OBJ Export가 끝난 뒤 COLMAP Export를 시작하십시오."
    if RUNTIME.import_in_progress:
        return "COLMAP 가져오기가 끝난 뒤 구조 통합 내보내기를 시작하십시오."
    if RUNTIME.workspace_setup_task is not None:
        return "작업공간 구성이 끝난 뒤 구조 통합 내보내기를 시작하십시오."
    from .layer_transform import layer_transform_active_block_reason

    transform_reason = layer_transform_active_block_reason()
    if transform_reason is not None:
        return "데이터 레이어 변환을 확정하거나 취소한 뒤 구조 통합 내보내기를 시작하십시오."
    if RUNTIME.modal_active or RUNTIME.marker_selection.has_draft:
        return "열린 마커 입력을 확정하거나 취소한 뒤 구조 통합 내보내기를 시작하십시오."
    from .marker_alignment import is_icp_input_locked, is_marker_preview_active

    if is_marker_preview_active() or is_icp_input_locked():
        return "마커 또는 ICP 미리보기를 정리한 뒤 구조 통합 내보내기를 시작하십시오."
    if not RUNTIME.model_catalog.active_models():
        return "A 그룹에 포함된 데이터 레이어가 없어 구조 통합 내보내기를 시작할 수 없습니다."
    if not any(
        str(getattr(record, "geometry_kind", GEOMETRY_KIND_COLMAP_POINT_CLOUD)).strip().upper()
        == GEOMETRY_KIND_COLMAP_POINT_CLOUD
        for record in RUNTIME.model_catalog.active_models()
    ):
        return "내보낼 COLMAP 레이어가 없습니다. OBJ 메시만 A 그룹에 있습니다."
    if RUNTIME.model_catalog.internal_anchor_model is None:
        return "A 그룹의 내부 Anchor를 확인할 수 없어 구조 통합 내보내기를 시작할 수 없습니다."
    if require_output_path:
        try:
            _normalized_output_root(
                bpy.context.scene.colmap_align_settings.merge_export_output_root
            )
        except ValueError as error:
            return str(error)
    return None


def _clear_merge_export_poll_timer(*, unregister: bool) -> None:
    """등록한 app timer 참조를 끊고 필요할 때 Blender 등록도 해제합니다."""

    callback = RUNTIME.merge_export_timer
    if unregister and callable(callback):
        try:
            if bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    RUNTIME.clear_merge_export_timer()


def stop_merge_export_poll_timer_for_lifecycle() -> None:
    """파일 load·등록 해제 전에 구조 통합 UI timer만 중지합니다."""

    _clear_merge_export_poll_timer(unregister=True)


def _set_merge_export_failed(message: str) -> None:
    """예외를 UI가 표시할 실패 상태와 원래 오류 문구로 전환합니다."""

    state = _merge_export_state()
    state.phase = MERGE_EXPORT_PHASE_FAILED
    state.last_error = str(message).strip()


def _discard_merge_export_job_after_poll_failure() -> None:
    """poll 예외 뒤 worker 후보를 최선으로 정리하고 Runtime 참조를 반드시 끊습니다."""

    state = _merge_export_state()
    backend = state.backend
    job = state.job
    try:
        if backend is not None and job is not None:
            backend.invalidate(job)
    except Exception:
        # 원래 poll 오류를 사용자에게 보존하기 위해 정리 실패는 상태 문구로 바꾸지 않습니다.
        pass
    finally:
        state.backend = None
        state.job = None


def _phase_name(phase: Any) -> str:
    """enum 또는 문자열 phase를 UI 상태 비교용 대문자 문자열로 정규화합니다."""

    return str(getattr(phase, "value", phase)).strip().upper()


def _issue_message(issue: Any, fallback: str) -> str:
    """구조화된 worker 진단에서 상태줄에 쓸 원인 문구를 반환합니다."""

    cause = str(getattr(issue, "cause", "")).strip()
    return cause or str(fallback).strip()


def _release_merge_export_job() -> None:
    """terminal worker의 job 폴더를 backend 소유 경계에서 정리합니다."""

    state = _merge_export_state()
    backend = state.backend
    job = state.job
    if backend is not None and job is not None:
        backend.invalidate(job)
    state.backend = None
    state.job = None


def _current_merge_runtime_fingerprint() -> str:
    """finalize 직전에 현재 A 그룹과 확정 변환을 다시 지문화합니다."""

    state = _merge_export_state()
    snapshot = state.snapshot
    if snapshot is None:
        raise RuntimeError("구조 통합 finalize에 필요한 입력 snapshot이 없습니다.")
    current = _build_merge_export_snapshot(
        Path(snapshot.output_root),
        output_format=str(snapshot.output_format.value),
        restore_import_axes=bool(snapshot.restore_import_axes),
        overwrite_existing_output=bool(snapshot.overwrite_existing_output),
    )
    return current.runtime_fingerprint


def _record_preflight_terminal(poll_result: Any) -> None:
    """preflight worker의 성공·실패 진단을 Runtime UI 상태로 옮깁니다."""

    state = _merge_export_state()
    preflight = getattr(poll_result, "preflight", None)
    if preflight is None:
        raise RuntimeError("구조 통합 preflight worker가 결과를 반환하지 않았습니다.")
    state.preflight = preflight
    state.progress = getattr(poll_result, "progress", None)
    if bool(getattr(preflight, "is_ready", False)):
        state.phase = MERGE_EXPORT_PHASE_PREFLIGHT_READY
        state.last_error = ""
        state.last_issue = None
    else:
        issue = next(iter(getattr(preflight, "errors", ())), None)
        state.phase = MERGE_EXPORT_PHASE_FAILED
        state.last_issue = issue
        state.last_error = _issue_message(issue, "구조 통합 사전 검사에 실패했습니다.")
        backend = state.backend
        diagnostic_getter = getattr(backend, "failure_diagnostic_path", None)
        diagnostic_path = diagnostic_getter() if callable(diagnostic_getter) else None
        state.last_diagnostic_path = (
            str(diagnostic_path)
            if diagnostic_path is not None and Path(diagnostic_path).is_file()
            else ""
        )


def _record_export_terminal(poll_result: Any) -> None:
    """취소·실패·stale worker 결과를 후보 정리 전 UI 상태로 옮깁니다."""

    state = _merge_export_state()
    phase_name = _phase_name(getattr(poll_result, "phase", "FAILED"))
    result = getattr(poll_result, "result", None)
    issue = getattr(poll_result, "error", None)
    if issue is None and result is not None:
        issue = getattr(result, "error", None)
    state.result = result
    state.last_issue = issue
    backend = state.backend
    diagnostic_path = None
    if backend is not None:
        diagnostic_getter = getattr(backend, "failure_diagnostic_path", None)
        if callable(diagnostic_getter):
            diagnostic_path = diagnostic_getter()
    state.last_diagnostic_path = (
        str(diagnostic_path)
        if diagnostic_path is not None and Path(diagnostic_path).is_file()
        else ""
    )
    if phase_name == "CANCELLED":
        state.phase = MERGE_EXPORT_PHASE_CANCELLED
        state.last_error = "구조 통합 내보내기 작업을 취소했습니다. 최종 출력은 만들지 않았습니다."
    else:
        state.phase = MERGE_EXPORT_PHASE_FAILED
        fallback = (
            "구조 통합 입력이 작업 중 변경되어 후보 출력을 폐기했습니다."
            if phase_name == "STALE"
            else "구조 통합 내보내기 worker가 실패했습니다."
        )
        state.last_error = _issue_message(issue, fallback)


def _finalize_merge_export() -> None:
    """worker 성공 뒤 최신 Runtime 지문을 검증하고 backend에 원자 승격을 맡깁니다."""

    state = _merge_export_state()
    backend = state.backend
    job = state.job
    if backend is None or job is None:
        raise RuntimeError("최종 확정할 구조 통합 worker가 없습니다.")
    state.phase = MERGE_EXPORT_PHASE_WAITING_FOR_FINALIZE
    current_fingerprint = _current_merge_runtime_fingerprint()
    result = backend.finalize(job, current_runtime_fingerprint=current_fingerprint)
    state.result = result
    state.last_issue = getattr(result, "error", None)
    if _phase_name(getattr(result, "phase", "")) != "COMPLETED":
        state.phase = MERGE_EXPORT_PHASE_FAILED
        state.last_error = _issue_message(
            state.last_issue,
            "구조 통합 후보 출력을 최종 폴더로 확정하지 못했습니다.",
        )
        state.backend = None
        state.job = None
        return
    state.phase = MERGE_EXPORT_PHASE_COMPLETED
    state.last_error = ""
    state.last_issue = None
    state.last_success_output_root = str(result.output_root)
    state.last_success_report_path = str(Path(result.output_root) / "merge_report.json")
    state.last_diagnostic_path = ""
    state.backend = None
    state.job = None


def _poll_merge_export_job_timer() -> float | None:
    """worker poll·preflight·원자 finalize를 처리하고 terminal 상태에서 timer를 정리합니다."""

    state = _merge_export_state()
    backend = state.backend
    job = state.job
    if backend is None or job is None:
        _clear_merge_export_poll_timer(unregister=False)
        return None
    try:
        poll_result = backend.poll(job)
        state.progress = getattr(poll_result, "progress", None)
        phase_name = _phase_name(getattr(poll_result, "phase", "FAILED"))
        if phase_name == "PREFLIGHT_RUNNING":
            state.phase = MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING
        elif phase_name == "PREFLIGHT_READY":
            _record_preflight_terminal(poll_result)
            _release_merge_export_job()
            _clear_merge_export_poll_timer(unregister=False)
            request_redraw()
            return None
        elif phase_name in {"EXPORT_RUNNING", "RUNNING"}:
            state.phase = MERGE_EXPORT_PHASE_EXPORT_RUNNING
        elif phase_name == "CANCEL_PENDING":
            state.phase = MERGE_EXPORT_PHASE_CANCEL_PENDING
        elif phase_name == "WAITING_FOR_FINALIZE":
            _finalize_merge_export()
            _clear_merge_export_poll_timer(unregister=False)
            request_redraw()
            return None
        elif phase_name in {"FAILED", "CANCELLED", "STALE"}:
            _record_export_terminal(poll_result)
            _release_merge_export_job()
            _clear_merge_export_poll_timer(unregister=False)
            request_redraw()
            return None
        elif phase_name == "COMPLETED":
            _finalize_merge_export()
            _clear_merge_export_poll_timer(unregister=False)
            request_redraw()
            return None
        else:
            raise RuntimeError(f"알 수 없는 구조 통합 worker phase입니다: {phase_name}")
    except Exception as error:
        _set_merge_export_failed(str(error))
        _discard_merge_export_job_after_poll_failure()
        _clear_merge_export_poll_timer(unregister=False)
        set_workspace_status_text(session_align_workspace(), str(error))
        request_redraw()
        return None
    request_redraw()
    return _POLL_INTERVAL_SECONDS


def _register_merge_export_poll_timer() -> None:
    """작업 하나에 전용 app timer 하나만 등록합니다."""

    callback = RUNTIME.merge_export_timer
    if callable(callback):
        try:
            if bpy.app.timers.is_registered(callback):
                return
        except (ReferenceError, RuntimeError, ValueError):
            pass
    RUNTIME.merge_export_timer = _poll_merge_export_job_timer
    bpy.app.timers.register(_poll_merge_export_job_timer, first_interval=_POLL_INTERVAL_SECONDS)


def _start_merge_export_job(
    context: Any,
    *,
    preflight_only: bool,
    output_format: str = "TEXT",
    restore_import_axes: bool = True,
    overwrite_existing_output: bool = False,
) -> None:
    """새 snapshot으로 preflight 또는 전체 export worker를 시작하고 timer를 등록합니다."""

    output_root = _normalized_output_root(
        context.scene.colmap_align_settings.merge_export_output_root,
        allow_existing=overwrite_existing_output,
    )
    backend_type, _snapshot_type, _phase_type, _policy_type, _source_type = _merge_export_api()
    snapshot = _build_merge_export_snapshot(
        output_root,
        output_format=output_format,
        restore_import_axes=restore_import_axes,
        overwrite_existing_output=overwrite_existing_output,
    )
    backend = backend_type()
    job = backend.start(snapshot, preflight_only=preflight_only)
    state = _merge_export_state()
    state.backend = backend
    state.job = job
    state.snapshot = snapshot
    state.preflight = None
    state.progress = None
    state.result = None
    state.last_issue = None
    state.last_error = ""
    state.last_diagnostic_path = ""
    state.phase = (
        MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING
        if preflight_only
        else MERGE_EXPORT_PHASE_EXPORT_RUNNING
    )
    _register_merge_export_poll_timer()


def invalidate_merge_export_for_lifecycle(reason: str) -> None:
    """load·unregister 전에 worker와 후보 출력만 정리하고 상태를 비웁니다."""

    state = _merge_export_state()
    backend = getattr(state, "backend", None)
    job = getattr(state, "job", None)
    if backend is not None and job is not None:
        backend.invalidate(job)
    stop_merge_export_poll_timer_for_lifecycle()
    state.clear(error_message=reason)
    request_redraw()


class COLMAPALIGN_OT_merge_export(bpy.types.Operator):
    """파일 탐색기로 출력 위치를 받은 뒤 구조 통합 worker를 실행합니다."""

    bl_idname = "colmap_align.merge_export"
    bl_label = "Export"
    bl_description = "저장 위치를 선택하고 A 그룹 전체를 새 COLMAP 데이터셋으로 통합합니다"
    bl_options = {"INTERNAL"}

    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    output_format: EnumProperty(
        name="Sparse Format",
        description="sparse/0에 기록할 COLMAP 모델 파일 형식",
        items=(
            ("TEXT", "TXT", "rigs, cameras, frames, images, points3D를 TXT로 기록합니다"),
            ("BINARY", "Binary", "rigs, cameras, frames, images, points3D를 Binary로 기록합니다"),
        ),
        default="TEXT",
    )
    restore_import_axes: BoolProperty(
        name="Import 축변환 되돌리기",
        description="정렬 결과를 Anchor의 원래 COLMAP 축으로 되돌려 내보냅니다",
        default=True,
    )
    overwrite_confirmed: BoolProperty(default=False, options={"HIDDEN"})
    pending_output_root: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """활성 세션에서 출력 위치 선택 전 조건이 충족될 때만 허용합니다."""

        return bool(
            is_session_active_for_window(context.window)
            and merge_export_start_block_reason(require_output_path=False) is None
        )

    def invoke(self, context, _event):
        """마지막 COLMAP 경로에서 파일 탐색기를 열고 출력 부모 위치를 선택받습니다."""

        reason = merge_export_start_block_reason(require_output_path=False)
        if reason is not None:
            _report_status(self, {"WARNING"}, reason)
            return {"CANCELLED"}
        if not self.properties.is_property_set("directory"):
            self.directory = str(_merge_export_browser_start_directory(context))
        self.overwrite_confirmed = False
        self.pending_output_root = ""
        if bpy.app.background:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        """파일 탐색기에는 형식을, 덮어쓰기 확인에는 영향과 복구 경계를 표시합니다."""

        if not self.overwrite_confirmed:
            self.layout.prop(self, "output_format", text="Sparse Format")
            self.layout.prop(self, "restore_import_axes")
            return
        output_root = Path(self.pending_output_root)
        self.layout.label(text=f"기존 폴더: {output_root}", icon="ERROR")
        self.layout.label(text="새 결과가 검증된 뒤 기존 COLMAP 데이터셋을 교체합니다.")
        self.layout.label(text="작업이 실패하거나 취소되면 기존 폴더는 유지됩니다.")

    def execute(self, context):
        """선택한 부모 아래 새 출력 경로를 정하고 자동 사전 검사와 내보내기를 시작합니다."""

        reason = merge_export_start_block_reason(require_output_path=False)
        if reason is not None:
            _report_status(self, {"WARNING"}, reason)
            return {"CANCELLED"}
        try:
            if self.overwrite_confirmed:
                output_root = _normalized_output_root(
                    self.pending_output_root,
                    allow_existing=True,
                )
                overwrite_existing_output = output_root.exists()
            else:
                selected_parent = str(self.directory).strip()
                if not selected_parent:
                    _report_status(
                        self,
                        {"ERROR"},
                        "COLMAP 데이터셋을 저장할 위치를 선택하십시오.",
                    )
                    return {"CANCELLED"}
                output_root = _normalized_output_root(
                    str(_output_root_under_parent(selected_parent)),
                    allow_existing=True,
                )
                if output_root.exists():
                    self.pending_output_root = str(output_root)
                    self.overwrite_confirmed = True
                    return context.window_manager.invoke_props_dialog(
                        self,
                        width=560,
                        title="기존 COLMAP 데이터셋을 덮어쓰시겠습니까?",
                        confirm_text="덮어쓰기",
                    )
                overwrite_existing_output = False
            context.scene.colmap_align_settings.merge_export_output_root = str(output_root)
            _start_merge_export_job(
                context,
                preflight_only=False,
                output_format=self.output_format,
                restore_import_axes=self.restore_import_axes,
                overwrite_existing_output=overwrite_existing_output,
            )
        except Exception as error:
            _set_merge_export_failed(str(error))
            _report_status(self, {"ERROR"}, str(error))
            request_redraw()
            return {"CANCELLED"}
        _report_status(
            self,
            {"INFO"},
            f"구조 통합 내보내기를 시작했습니다: {output_root}",
        )
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_cancel_merge_export(bpy.types.Operator):
    """실행 중인 구조 통합 worker에 취소 요청을 전달합니다."""

    bl_idname = "colmap_align.cancel_merge_export"
    bl_label = "작업 취소"
    bl_description = "구조 통합 작업을 취소하고 후보 출력만 정리합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 세션의 취소 가능한 구조 통합 작업에서만 버튼을 활성화합니다."""

        return bool(is_session_active_for_window(context.window) and is_merge_export_running())

    def execute(self, _context):
        """worker 취소 sentinel을 요청하고 기존 poll timer가 종료를 확인하게 둡니다."""

        state = _merge_export_state()
        backend = state.backend
        job = state.job
        if backend is None or job is None:
            _report_status(self, {"WARNING"}, "취소할 구조 통합 작업이 없습니다.")
            return {"CANCELLED"}
        try:
            backend.cancel(job)
        except Exception as error:
            _set_merge_export_failed(str(error))
            _report_status(self, {"ERROR"}, str(error))
            request_redraw()
            return {"CANCELLED"}
        state.phase = MERGE_EXPORT_PHASE_CANCEL_PENDING
        _register_merge_export_poll_timer()
        _report_status(self, {"INFO"}, "구조 통합 취소를 요청했습니다. worker 종료를 확인하는 중입니다.")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_open_merge_export_output(bpy.types.Operator):
    """마지막 성공 구조 통합 출력 폴더를 운영체제 파일 탐색기로 엽니다."""

    bl_idname = "colmap_align.open_merge_export_output"
    bl_label = "폴더 열기"
    bl_description = "마지막 성공한 구조 통합 출력 폴더를 엽니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """마지막 성공 출력 폴더가 실제로 존재할 때만 열기를 허용합니다."""

        output_root = str(_merge_export_state().last_success_output_root).strip()
        return bool(output_root and Path(output_root).is_dir())

    def execute(self, _context):
        """Blender의 안전한 경로 열기 오퍼레이터에 검증된 폴더를 전달합니다."""

        result = bpy.ops.wm.path_open(filepath=_merge_export_state().last_success_output_root)
        return result if "FINISHED" in result else {"CANCELLED"}


class COLMAPALIGN_OT_open_merge_export_report(bpy.types.Operator):
    """마지막 성공 구조 통합 감사 보고서를 운영체제 기본 앱으로 엽니다."""

    bl_idname = "colmap_align.open_merge_export_report"
    bl_label = "보고서 보기"
    bl_description = "마지막 성공한 구조 통합 merge_report.json을 엽니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """마지막 성공 보고서 파일이 실제로 존재할 때만 열기를 허용합니다."""

        report_path = str(_merge_export_state().last_success_report_path).strip()
        return bool(report_path and Path(report_path).is_file())

    def execute(self, _context):
        """Blender의 안전한 경로 열기 오퍼레이터에 검증된 보고서를 전달합니다."""

        result = bpy.ops.wm.path_open(filepath=_merge_export_state().last_success_report_path)
        return result if "FINISHED" in result else {"CANCELLED"}


CLASSES = (
    COLMAPALIGN_OT_merge_export,
    COLMAPALIGN_OT_cancel_merge_export,
    COLMAPALIGN_OT_open_merge_export_output,
    COLMAPALIGN_OT_open_merge_export_report,
)
