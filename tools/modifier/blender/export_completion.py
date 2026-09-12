"""Export Core 결과를 Blender Runtime과 사용자 상태에 반영합니다."""

from __future__ import annotations

from ..core.export_types import ExportTransactionError
from ..core.edit_journal import EditJournal
from .camera_edit_session import force_end_camera_mode
from .camera_review_session import ensure_review_session
from .dataset_adapter import refresh_shared_source_after_export
from .point_edit_session import force_end_point_mode
from .runtime_state import (
    state_from_successful_export,
    write_domain_settings,
    write_history_settings,
    write_source_settings,
)
from .modifier_runtime_domain import domain_for_scene
from .selection import clear_snapshots


def export_error_message(exc: Exception) -> str:
    """트랜잭션 실패 시 자동 복구 여부와 수동 복구 경로를 함께 표시합니다."""

    if not isinstance(exc, ExportTransactionError):
        return str(exc)
    details = [str(exc)]
    if exc.backup_snapshot is not None:
        details.append(f"Backup: {exc.backup_snapshot}")
    if exc.recovery_directory is not None:
        details.append(f"복구 자료: {exc.recovery_directory}")
    if exc.source_changed:
        details.append("원본 일부가 변경되었을 수 있어 수동 확인이 필요합니다.")
    elif exc.rollback_attempted and exc.rollback_succeeded:
        details.append("원본은 자동 rollback으로 복구되었습니다.")
    return " | ".join(details)


def commit_exported_runtime(context, state, result) -> tuple[str, ...]:
    """디스크 저장 성공 결과를 Blender Runtime의 새 기준선으로 전환합니다."""

    settings = context.scene.colmap_import
    domain = domain_for_scene(context.scene, required=True)
    if domain.dataset_state is not state:
        raise ValueError("Export 완료 대상과 현재 Modifier Runtime Domain이 다릅니다.")
    exported_state = state_from_successful_export(state, result.loaded)
    exported_journal = EditJournal(
        exported_state.dataset_uuid,
        exported_state.source.source_fingerprint,
    )
    try:
        domain.replace_baseline(
            exported_state,
            exported_journal,
            status="Export 결과를 새 Modifier 기준선으로 설치했습니다.",
            mark_dirty=True,
        )
        write_source_settings(settings, exported_state)
        write_history_settings(settings, exported_state)
        write_domain_settings(settings, domain)
        settings.camera_count = str(
            len(exported_state.current_reconstruction.cameras)
        )
        settings.image_count = str(len(exported_state.current_reconstruction.images))
        settings.point3d_count = str(
            len(exported_state.current_reconstruction.points3d)
        )
    except Exception as exc:
        domain.freeze(
            "Export 파일은 기록됐지만 새 Runtime 기준선을 설치하지 못했습니다: "
            f"{exc}"
        )
        write_domain_settings(settings, domain)
        raise RuntimeError(
            "Export 파일 기록 뒤 Modifier Runtime 기준선 설치에 실패했습니다. "
            "Dataset을 다시 Import하십시오. "
            f"원인: {exc}"
        ) from exc
    refresh_shared_source_after_export(
        context.scene,
        result.loaded,
        exported_state.current_reconstruction,
        settings,
    )

    clear_snapshots()
    force_end_point_mode(context, exported_state.dataset_uuid)
    force_end_camera_mode(context, exported_state.dataset_uuid)
    settings.selected_point_count = 0
    settings.observing_camera_count = 0
    settings.derived_orphan_count = 0
    settings.camera_target_count = 0
    settings.remove_target = "NONE"
    settings.point_selection_status = (
        "Export가 완료되었습니다. 새 편집 작업을 시작할 수 있습니다."
    )

    warnings = list(result.loaded.warnings)
    warnings.extend(result.cleanup_warnings)
    try:
        if exported_state.current_reconstruction is not state.current_reconstruction:
            from .edit_coordinator import synchronize_scene

            warnings.extend(synchronize_scene(context, exported_state, settings))
        ensure_review_session(context, state=exported_state)
    except Exception as exc:
        warnings.append(
            "파일 Export는 완료되었지만 Camera Review 상태 갱신에 실패했습니다: "
            f"{exc}"
        )
    return tuple(warnings)


def finish_export_success(context, state, result) -> tuple[str, ...]:
    """동기·Modal 경로에 동일한 성공 Status 계약을 적용합니다."""

    settings = context.scene.colmap_import
    warnings = commit_exported_runtime(context, state, result)
    backup_text = (
        f", Backup: {result.backup_snapshot}"
        if result.backup_snapshot is not None
        else ", Backup: 사용 안 함"
    )
    settings.warning_summary = "\n".join(warnings)
    format_text = (
        "TXT"
        if result.loaded.source.input_format == "text"
        else "BIN"
    )
    settings.export_status = (
        f"Export 완료 ({format_text}): Camera {settings.camera_count}, "
        f"Image {settings.image_count}, Point3D {settings.point3d_count}"
        f"{backup_text}"
    )
    return warnings
