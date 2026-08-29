"""Blender 상태 변경을 역순 복구하는 멱등 Transaction Journal을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class TransactionState(str, Enum):
    """Transaction의 단방향 수명주기 상태를 정의합니다."""

    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


@dataclass(slots=True)
class RollbackWarning:
    """Rollback 중 발생한 보조 오류를 원래 예외와 분리해 보관합니다."""

    label: str
    message: str


@dataclass(slots=True)
class _RollbackAction:
    """한 번만 실행되는 역순 복구 동작을 보관합니다."""

    label: str
    callback: Callable[[], None]


class TransactionJournal:
    """부분 변경을 실패 시 역순으로 되돌리는 멱등 Journal입니다."""

    def __init__(self, label: str) -> None:
        """빈 Transaction Journal을 생성합니다."""

        self.label = str(label)
        self.state = TransactionState.OPEN
        self._actions: list[_RollbackAction] = []
        self.warnings: list[RollbackWarning] = []

    @property
    def is_open(self) -> bool:
        """Journal이 아직 Commit 또는 Abort되지 않았는지 반환합니다."""

        return self.state is TransactionState.OPEN

    def add_rollback(self, label: str, callback: Callable[[], None]) -> None:
        """실패 시 실행할 복구 동작을 Journal 끝에 추가합니다."""

        if not self.is_open:
            raise RuntimeError("종료된 Transaction에는 복구 동작을 추가할 수 없습니다.")
        self._actions.append(_RollbackAction(str(label), callback))

    def record_attribute(self, target: Any, name: str) -> None:
        """객체 속성의 현재 값을 실패 시 복구하도록 기록합니다."""

        original = getattr(target, name)

        def restore() -> None:
            """기록한 객체 속성 값을 복구합니다."""

            setattr(target, name, original)

        self.add_rollback(f"{name} 속성 복구", restore)

    def record_mapping_value(self, target: Any, key: str) -> None:
        """Mapping 항목의 존재 여부와 값을 실패 시 복구하도록 기록합니다."""

        existed = key in target
        original = target.get(key) if existed else None

        def restore() -> None:
            """기록한 Mapping 항목을 원래 상태로 복구합니다."""

            if existed:
                target[key] = original
            elif key in target:
                del target[key]

        self.add_rollback(f"{key} 항목 복구", restore)

    def commit(self) -> None:
        """복구 동작을 폐기하고 Transaction을 성공으로 확정합니다."""

        if self.state is TransactionState.COMMITTED:
            return
        if self.state is TransactionState.ABORTED:
            raise RuntimeError("Abort된 Transaction은 Commit할 수 없습니다.")
        self._actions.clear()
        self.state = TransactionState.COMMITTED

    def abort(self) -> tuple[RollbackWarning, ...]:
        """등록된 복구 동작을 역순 실행하고 경고를 반환합니다."""

        if self.state is TransactionState.ABORTED:
            return tuple(self.warnings)
        if self.state is TransactionState.COMMITTED:
            return tuple(self.warnings)
        while self._actions:
            action = self._actions.pop()
            try:
                action.callback()
            except Exception as exc:
                self.warnings.append(
                    RollbackWarning(action.label, f"{type(exc).__name__}: {exc}")
                )
        self.state = TransactionState.ABORTED
        return tuple(self.warnings)

    def __enter__(self) -> TransactionJournal:
        """Context Manager 진입 시 현재 Journal을 반환합니다."""

        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        """예외가 있으면 Abort하고 없으면 명시적 Commit을 요구합니다."""

        if exc_type is not None or self.is_open:
            self.abort()
        return False
