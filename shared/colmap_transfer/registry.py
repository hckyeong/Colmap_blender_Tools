"""도구 직접 Import 없이 COLMAP 값 Snapshot 제공자를 연결합니다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import ColmapTransferSnapshot, validate_snapshot


Provider = Callable[[Any], ColmapTransferSnapshot]
Validator = Callable[[Any, ColmapTransferSnapshot], bool]
HandleFactory = Callable[[Any], "SnapshotProviderHandle"]


class ProviderUnavailableError(RuntimeError):
    """등록된 Modifier Snapshot 제공자가 없음을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class ProviderProgress:
    """도구 경계를 넘는 Snapshot 준비 단계와 단조 증가 비율을 보관합니다."""

    phase: str
    fraction: float
    completed_units: int = 0
    total_units: int = 0
    message: str = ""


class SnapshotProviderHandle(Protocol):
    """Timer와 동기 루프가 함께 소비하는 증분 Snapshot 제공자 계약입니다."""

    done: bool
    failed: bool
    progress: ProviderProgress
    result: ColmapTransferSnapshot | None
    error: Exception | None

    def advance(self, work_budget_ms: float = 20.0) -> ProviderProgress:
        """주어진 시간 예산만큼 값을 준비하고 최신 진행 상태를 반환합니다."""

    def abort(self) -> None:
        """미완료 준비 작업을 취소하고 부분 결과를 노출하지 않습니다."""


@dataclass(frozen=True, slots=True)
class _ProviderEntry:
    """동기 호환 Callback과 선택적 증분 Handle Factory를 함께 보관합니다."""

    provider: Provider
    validator: Validator
    handle_factory: HandleFactory | None

    def __getitem__(self, index: int) -> Any:
        """구버전 테스트·호출자가 사용한 `(provider, validator)` 인덱싱을 유지합니다."""

        return (self.provider, self.validator, self.handle_factory)[int(index)]


class _SynchronousProviderHandle:
    """기존 동기 Provider를 증분 소비자에 연결하는 단일 단계 호환 Handle입니다."""

    def __init__(self, context: Any, provider: Provider) -> None:
        """실행 전 Context와 Provider를 보관하고 부분 결과를 비웁니다."""

        self._context = context
        self._provider = provider
        self.done = False
        self.failed = False
        self.result: ColmapTransferSnapshot | None = None
        self.error: Exception | None = None
        self.progress = ProviderProgress("PREPARE", 0.0, message="Snapshot 준비 중")

    def advance(self, work_budget_ms: float = 20.0) -> ProviderProgress:
        """기존 Provider를 한 번 실행해 동기 호환 Snapshot을 확정합니다."""

        del work_budget_ms
        if self.done or self.failed:
            return self.progress
        try:
            self.result = validate_snapshot(self._provider(self._context))
        except Exception as exc:
            self.failed = True
            self.error = exc
            self.progress = ProviderProgress("FAILED", 0.0, message=str(exc))
            raise
        self.done = True
        self.progress = ProviderProgress("DONE", 1.0, 1, 1, "Snapshot 준비 완료")
        return self.progress

    def abort(self) -> None:
        """실행 전 동기 호환 Handle만 취소 상태로 전환합니다."""

        if self.done or self.failed:
            return
        self.failed = True
        self.error = RuntimeError("Snapshot 준비가 취소되었습니다.")
        self.progress = ProviderProgress("CANCELLED", 0.0, message=str(self.error))


_PROVIDERS: dict[str, _ProviderEntry] = {}


def _provider_entry(value: Any) -> _ProviderEntry:
    """내부 Registry의 구버전 2-Tuple 값을 현행 Provider Entry로 정규화합니다."""

    if isinstance(value, _ProviderEntry):
        return value
    if isinstance(value, tuple) and len(value) in {2, 3}:
        return _ProviderEntry(
            value[0],
            value[1],
            value[2] if len(value) == 3 else None,
        )
    raise RuntimeError("COLMAP Snapshot 제공자 Registry 값이 손상되었습니다.")


def register_provider(
    name: str,
    provider: Provider,
    validator: Validator,
    *,
    handle_factory: HandleFactory | None = None,
) -> None:
    """이름별 Snapshot 생성·재검증 Callback을 중복 없이 등록합니다."""

    key = str(name).strip()
    if (
        not key
        or not callable(provider)
        or not callable(validator)
        or (handle_factory is not None and not callable(handle_factory))
    ):
        raise ValueError("COLMAP Snapshot 제공자 계약이 올바르지 않습니다.")
    current = _PROVIDERS.get(key)
    candidate = _ProviderEntry(provider, validator, handle_factory)
    if current is not None and current != candidate:
        raise RuntimeError(f"COLMAP Snapshot 제공자가 이미 등록되었습니다: {key}")
    _PROVIDERS[key] = candidate


def unregister_provider(name: str) -> None:
    """지정한 Snapshot 제공자만 등록 해제합니다."""

    _PROVIDERS.pop(str(name).strip(), None)


def provider_available(name: str = "modifier") -> bool:
    """지정한 값 Snapshot 제공자의 등록 여부를 반환합니다."""

    return str(name).strip() in _PROVIDERS


def build_snapshot(context: Any, name: str = "modifier") -> ColmapTransferSnapshot:
    """등록 제공자에서 새 불변 Snapshot을 만들고 계약을 검증합니다."""

    stored = _PROVIDERS.get(str(name).strip())
    if stored is None:
        raise ProviderUnavailableError("COLMAP Modifier Snapshot 제공자가 없습니다.")
    callbacks = _provider_entry(stored)
    return validate_snapshot(callbacks.provider(context))


def open_snapshot_provider(
    context: Any,
    name: str = "modifier",
) -> SnapshotProviderHandle:
    """등록된 증분 Handle을 열고 기존 Provider는 단일 단계 Handle로 감쌉니다."""

    stored = _PROVIDERS.get(str(name).strip())
    if stored is None:
        raise ProviderUnavailableError("COLMAP Modifier Snapshot 제공자가 없습니다.")
    callbacks = _provider_entry(stored)
    if callbacks.handle_factory is not None:
        return callbacks.handle_factory(context)
    return _SynchronousProviderHandle(context, callbacks.provider)


def validate_provider_snapshot(
    context: Any,
    snapshot: ColmapTransferSnapshot,
    name: str = "modifier",
) -> bool:
    """Staging 전후 제공자 상태가 Snapshot token과 같은지 반환합니다."""

    stored = _PROVIDERS.get(str(name).strip())
    if stored is None:
        return False
    callbacks = _provider_entry(stored)
    validate_snapshot(snapshot)
    return bool(callbacks.validator(context, snapshot))


def validate_open_provider_snapshot(
    context: Any,
    handle: SnapshotProviderHandle,
    snapshot: ColmapTransferSnapshot,
    name: str = "modifier",
) -> bool:
    """Registry가 연 증분 Handle의 동일한 불변 결과와 최신 Source token만 검사합니다."""

    stored = _PROVIDERS.get(str(name).strip())
    if stored is None:
        return False
    if (
        not bool(getattr(handle, "done", False))
        or bool(getattr(handle, "failed", False))
        or getattr(handle, "result", None) is not snapshot
        or not isinstance(snapshot, ColmapTransferSnapshot)
        or not str(snapshot.content_fingerprint or "")
    ):
        return False
    callbacks = _provider_entry(stored)
    return bool(callbacks.validator(context, snapshot))


__all__ = [
    "ProviderUnavailableError",
    "ProviderProgress",
    "SnapshotProviderHandle",
    "build_snapshot",
    "open_snapshot_provider",
    "provider_available",
    "register_provider",
    "unregister_provider",
    "validate_provider_snapshot",
    "validate_open_provider_snapshot",
]
