"""Export Core 결과를 Blender Runtime과 사용자 상태에 반영합니다."""

from __future__ import annotations

from ..core.export_types import ExportTransactionError
from .camera_edit_session import force_end_camera_mode
from .camera_review_session import ensure_review_session
from .point_edit_session import force_end_point_mode
from .runtime_state import (
    install_state,
    state_from_successful_export,
    write_history_settings,
    write_source_settings,
)
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
    exported_state = state_from_successful_export(state, result.loaded)
    install_state(exported_state)
    write_source_settings(settings, exported_state)
    write_history_settings(settings, exported_state)
    settings.camera_count = str(
        len(exported_state.current_reconstruction.cameras)
    )
    settings.image_count = str(len(exported_state.current_reconstruction.images))
    settings.point3d_count = str(
        len(exported_state.current_reconstruction.points3d)
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
