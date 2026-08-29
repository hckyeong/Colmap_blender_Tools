"""COLMAP 장면·Runtime 변경 Operator의 공통 실행 잠금을 제공합니다."""

from __future__ import annotations


def export_mutation_allowed(context) -> bool:
    """Import 또는 Export 진행 작업이 없을 때만 상태 변경을 허용합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if settings is None or settings.export_running:
        return False
    from .import_progress_session import active_import_progress_session

    return active_import_progress_session() is None


def require_export_mutation_allowed(context, operation_name: str) -> None:
    """확인창과 실행 사이에도 공통 작업 잠금이 유지되는지 재검증합니다."""

    if not export_mutation_allowed(context):
        settings = getattr(getattr(context, "scene", None), "colmap_import", None)
        if settings is not None and settings.export_running:
            operation = "Export"
        else:
            operation = "Import"
        raise ValueError(f"{operation} 진행 중에는 {operation_name}을(를) 실행할 수 없습니다.")
