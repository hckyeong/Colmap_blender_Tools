"""Blender 전역 Undo와 분리된 COLMAP Align 마커 이력을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _snapshots_equal(before: Any, after: Any) -> bool:
    """배열을 포함할 수 있는 snapshot의 동등 비교 실패를 안전하게 변경으로 처리합니다."""

    try:
        return bool(before == after)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class SnapshotCommand:
    """변경 전후 스냅샷과 사용자용 동작 이름을 보관합니다."""

    action: str
    before: Any
    after: Any
    kind: str = ""


@dataclass
class DraftScope:
    """확정 전 입력만 되돌리는 임시 Undo 범위를 보관합니다."""

    baseline: Any
    undo_entries: list[SnapshotCommand] = field(default_factory=list)
    redo_entries: list[SnapshotCommand] = field(default_factory=list)


class UndoJournal:
    """드래프트 입력과 확정 세션 명령을 시간순 단일 스택으로 관리합니다."""

    def __init__(self) -> None:
        """비어 있는 확정·드래프트 Undo/Redo 스택을 준비합니다."""
        self._undo_entries: list[SnapshotCommand] = []
        self._redo_entries: list[SnapshotCommand] = []
        self._scope: DraftScope | None = None
        self.last_action = ""
        self.last_kind = ""

    @property
    def undo_count(self) -> int:
        """확정 마커 쌍에 적용할 수 있는 Undo 개수를 반환합니다."""
        return len(self._undo_entries)

    @property
    def redo_count(self) -> int:
        """확정 마커 쌍에 적용할 수 있는 Redo 개수를 반환합니다."""
        return len(self._redo_entries)

    @property
    def draft_undo_count(self) -> int:
        """현재 드래프트 범위에서 되돌릴 입력 횟수를 반환합니다."""
        return len(self._scope.undo_entries) if self._scope is not None else 0

    @property
    def draft_redo_count(self) -> int:
        """현재 드래프트 범위에서 다시 실행할 입력 횟수를 반환합니다."""
        return len(self._scope.redo_entries) if self._scope is not None else 0

    @property
    def scope_active(self) -> bool:
        """확정되지 않은 드래프트 Undo 범위가 열려 있는지 반환합니다."""
        return self._scope is not None

    @property
    def can_undo(self) -> bool:
        """확정 마커 이력에 하나 이상의 Undo 명령이 있는지 반환합니다."""
        return bool(self._undo_entries)

    @property
    def can_redo(self) -> bool:
        """확정 마커 이력에 하나 이상의 Redo 명령이 있는지 반환합니다."""
        return bool(self._redo_entries)

    @property
    def next_redo_kind(self) -> str:
        """다음 확정 Redo가 복원할 명령 종류를 비파괴로 반환합니다."""

        return self._redo_entries[-1].kind if self._redo_entries else ""

    @property
    def can_undo_draft(self) -> bool:
        """드래프트 입력 이력에 Undo 가능한 명령이 있는지 반환합니다."""
        return self.draft_undo_count > 0

    @property
    def can_redo_draft(self) -> bool:
        """드래프트 입력 이력에 Redo 가능한 명령이 있는지 반환합니다."""
        return self.draft_redo_count > 0

    def begin_scope(self, baseline_snapshot: Any) -> bool:
        """확정 전 마커 후보를 위한 임시 Undo 범위를 시작합니다."""
        if self._scope is not None:
            return False
        self._scope = DraftScope(baseline=baseline_snapshot)
        self.last_action = ""
        return True

    def record_draft(self, before: Any, after: Any, action: str) -> bool:
        """성공한 후보 선택 또는 재선택을 드래프트 이력에 기록합니다."""
        scope = self._scope
        if scope is None or _snapshots_equal(before, after):
            return False
        scope.undo_entries.append(SnapshotCommand(action=action, before=before, after=after))
        scope.redo_entries.clear()
        self.last_action = action
        self.last_kind = ""
        return True

    def undo_draft(self, state: Any) -> bool:
        """현재 드래프트에서 마지막 점 선택만 원자적으로 되돌립니다."""
        scope = self._scope
        if scope is None or not scope.undo_entries:
            self.last_action = "되돌릴 현재 마커 입력이 없습니다."
            return False
        command = scope.undo_entries[-1]
        state.restore_snapshot(command.before)
        scope.undo_entries.pop()
        scope.redo_entries.append(command)
        self.last_action = f"{command.action} 되돌림"
        self.last_kind = ""
        return True

    def redo_draft(self, state: Any) -> bool:
        """드래프트에서 되돌린 마지막 점 선택을 원자적으로 다시 실행합니다."""
        scope = self._scope
        if scope is None or not scope.redo_entries:
            self.last_action = "다시 실행할 현재 마커 입력이 없습니다."
            return False
        command = scope.redo_entries[-1]
        state.restore_snapshot(command.after)
        scope.redo_entries.pop()
        scope.undo_entries.append(command)
        self.last_action = f"{command.action} 다시 실행"
        self.last_kind = ""
        return True

    def cancel_scope(self, state: Any) -> bool:
        """드래프트 시작 전 상태를 복원하고 확정 Redo 이력은 유지합니다."""
        scope = self._scope
        if scope is None:
            return False
        state.restore_snapshot(scope.baseline)
        self._scope = None
        self.last_action = "마커 쌍 입력 취소"
        self.last_kind = ""
        return True

    def commit_scope(self, final_snapshot: Any, action: str, kind: str) -> bool:
        """드래프트 전체를 하나의 확정 명령으로 압축하고 실패 시 범위를 보존합니다."""
        scope = self._scope
        if scope is None:
            return False
        if _snapshots_equal(scope.baseline, final_snapshot):
            self.last_action = "변경된 마커 쌍이 없습니다."
            return False
        command = SnapshotCommand(
            action=action,
            before=scope.baseline,
            after=final_snapshot,
            kind=kind,
        )
        self._undo_entries.append(command)
        self._redo_entries.clear()
        self._scope = None
        self.last_action = action
        self.last_kind = kind
        return True

    def record_committed(self, before: Any, after: Any, action: str, kind: str = "") -> bool:
        """삭제처럼 드래프트 없이 끝난 확정 변경을 이력 한 건으로 기록합니다."""
        if self._scope is not None or _snapshots_equal(before, after):
            return False
        self._undo_entries.append(
            SnapshotCommand(action=action, before=before, after=after, kind=kind)
        )
        self._redo_entries.clear()
        self.last_action = action
        self.last_kind = kind
        return True

    def undo_committed(self, state: Any) -> bool:
        """마지막 확정 세션 명령을 원자적으로 되돌립니다."""
        if not self._undo_entries:
            self.last_action = "되돌릴 COLMAP Align 작업이 없습니다."
            self.last_kind = ""
            return False
        command = self._undo_entries[-1]
        self._restore_committed_snapshot(state, command.before, command.kind)
        self._undo_entries.pop()
        self._redo_entries.append(command)
        self.last_action = f"{command.action} 되돌림"
        self.last_kind = command.kind
        return True

    def redo_committed(self, state: Any) -> bool:
        """마지막 확정 세션 Redo 명령을 원자적으로 다시 실행합니다."""
        if not self._redo_entries:
            self.last_action = "다시 실행할 COLMAP Align 작업이 없습니다."
            self.last_kind = ""
            return False
        command = self._redo_entries[-1]
        self._restore_committed_snapshot(state, command.after, command.kind)
        self._redo_entries.pop()
        self._undo_entries.append(command)
        self.last_action = f"{command.action} 다시 실행"
        self.last_kind = command.kind
        return True

    @staticmethod
    def _restore_committed_snapshot(state: Any, snapshot: Any, kind: str) -> None:
        """명령 종류별 복원 함수 또는 기존 상태 객체에 스냅샷 복원을 위임합니다."""
        if callable(state):
            state(snapshot, kind)
            return
        state.restore_snapshot(snapshot)

    def clear(self) -> None:
        """세션 종료 시 확정·드래프트 이력과 상태 문구를 모두 비웁니다."""
        self._undo_entries.clear()
        self._redo_entries.clear()
        self._scope = None
        self.last_action = ""
        self.last_kind = ""
