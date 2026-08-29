"""구조 통합 내보내기의 작은 공개 타입과 지연 backend import를 제공합니다."""

from __future__ import annotations

from .types import (
    MergeExportError,
    MergeExportPollResult,
    MergeExportPreflightResult,
    MergeExportProgress,
    MergeExportResult,
    MergeExportSnapshot,
    MergeIssue,
    MergePhase,
    MergePolicy,
    MergePreflightError,
    MergeProtocolError,
    MergeSourceModelSnapshot,
    MergeStage,
    MergeStaleResultError,
)


def __getattr__(name: str):
    """UI가 실제 backend를 요청할 때만 subprocess 모듈을 지연 import합니다."""

    if name in {"MergeExportBackend", "MergeExportBackendError", "MergeExportJob"}:
        from .backend import MergeExportBackend, MergeExportBackendError, MergeExportJob

        return {
            "MergeExportBackend": MergeExportBackend,
            "MergeExportBackendError": MergeExportBackendError,
            "MergeExportJob": MergeExportJob,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = (
    "MergeExportBackend",
    "MergeExportBackendError",
    "MergeExportError",
    "MergeExportJob",
    "MergeExportPollResult",
    "MergeExportPreflightResult",
    "MergeExportProgress",
    "MergeExportResult",
    "MergeExportSnapshot",
    "MergeIssue",
    "MergePhase",
    "MergePolicy",
    "MergePreflightError",
    "MergeProtocolError",
    "MergeSourceModelSnapshot",
    "MergeStage",
    "MergeStaleResultError",
)
