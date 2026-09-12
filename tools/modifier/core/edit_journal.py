"""Modifier 전용 warm Undo·Redo command stack을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from .edit_history import dataset_transform_from_history, operation_from_dict, serialize_history
from .edit_types import TransformOperation
import json

from .edit_delta import (
    EditDelta,
    EditStateToken,
    apply_forward,
    apply_inverse,
    capture_edit_delta,
)
from .edit_types import (
    EDIT_SCHEMA_VERSION,
    EditOperation,
    ImageObservationRemovalPlan,
    PointRemovalPlan,
)
from .indexes import RelationshipIndexes
from .model import Reconstruction


RemovalPlan = PointRemovalPlan | ImageObservationRemovalPlan


@dataclass(frozen=True)
class EditCommand:
    """저장 operation과 warm Delta 및 양쪽 revision을 한 단위로 묶습니다."""

    operation: EditOperation
    delta: EditDelta
    previous_revision: int
    resulting_revision: int

    def __post_init__(self) -> None:
        """Operation·Delta·command의 token과 revision 일치를 검증합니다."""

        if (
            not isinstance(self.operation, EditOperation)
            or self.operation.schema_version != EDIT_SCHEMA_VERSION
        ):
            raise ValueError(
                "Legacy schema 1 작업은 신규 warm Delta command로 만들 수 없습니다."
            )
        if self.resulting_revision != self.previous_revision + 1:
            raise ValueError("Journal command revision이 연속적이지 않습니다.")
        operation_boundary = (
            self.operation.operation_type,
            self.operation.dataset_uuid,
            self.operation.source_fingerprint,
            self.operation.previous_revision,
            self.operation.resulting_revision,
        )
        delta_boundary = (
            self.delta.operation_type,
            self.delta.dataset_uuid,
            self.delta.source_fingerprint,
            self.delta.previous_revision,
            self.delta.resulting_revision,
        )
        command_boundary = (
            self.operation.operation_type,
            self.operation.dataset_uuid,
            self.operation.source_fingerprint,
            self.previous_revision,
            self.resulting_revision,
        )
        if operation_boundary != delta_boundary or operation_boundary != command_boundary:
            raise ValueError("Operation·Delta·command의 token 또는 revision이 다릅니다.")


@dataclass(frozen=True)
class TransformCommand:
    """점·카메라 배열을 소유하지 않는 순수 변환 명령입니다."""

    operation: TransformOperation
    previous_revision: int
    resulting_revision: int

    def __post_init__(self) -> None:
        """저장 스키마와 명령의 개정 경계가 동일한지 검증합니다."""
        if not isinstance(self.operation, TransformOperation):
            raise TypeError("변환 명령에는 변환 작업이 필요합니다.")
        operation_from_dict(json.loads(serialize_history((self.operation,)))[0])
        if (self.previous_revision, self.resulting_revision) != (
            self.operation.previous_revision, self.operation.resulting_revision
        ):
            raise ValueError("변환 명령과 작업의 개정 경계가 다릅니다.")


def create_transform_command(operation: TransformOperation) -> TransformCommand:
    """확정 변환 작업으로 배열 복사 없는 명령을 생성합니다."""
    return TransformCommand(operation, operation.previous_revision, operation.resulting_revision)


def _apply_command(reconstruction, indexes, command, token, *, forward):
    """삭제는 델타로 적용하고 변환은 형상을 유지한 채 개정만 이동합니다."""
    if isinstance(command, TransformCommand):
        operation = command.operation
        expected = command.previous_revision if forward else command.resulting_revision
        if (token.dataset_uuid != operation.dataset_uuid
                or token.source_fingerprint != operation.source_fingerprint
                or token.edit_revision != expected or token.index_revision != expected):
            raise ValueError("변환 명령과 현재 상태의 개정 또는 원본이 다릅니다.")
        revision = command.resulting_revision if forward else command.previous_revision
        return EditStateToken(token.dataset_uuid, token.source_fingerprint, revision, revision)
    apply = apply_forward if forward else apply_inverse
    return apply(reconstruction, indexes, command.delta, token)


def _apply_forward(reconstruction, indexes, command, token):
    """명령을 정방향으로 적용합니다."""
    return _apply_command(reconstruction, indexes, command, token, forward=True)


def _apply_inverse(reconstruction, indexes, command, token):
    """명령을 역방향으로 적용합니다."""
    return _apply_command(reconstruction, indexes, command, token, forward=False)


@dataclass(frozen=True)
class ResetCommand:
    """현재 적용된 편집 Command 묶음을 같은 세션에서만 되돌릴 Reset으로 보관합니다."""

    commands: tuple[EditCommand | TransformCommand, ...]
    previous_revision: int

    def __post_init__(self) -> None:
        """Reset 전 Command가 0부터 현재 revision까지 연속적인지 검증합니다."""

        if not self.commands:
            raise ValueError("빈 편집 이력으로 Reset Command를 만들 수 없습니다.")
        if self.previous_revision != len(self.commands):
            raise ValueError("Reset 전 revision과 Command 수가 일치하지 않습니다.")
        for expected_revision, command in enumerate(self.commands):
            if not isinstance(command, (EditCommand, TransformCommand)):
                raise TypeError("Reset Command에는 EditCommand만 포함할 수 있습니다.")
            if (
                command.previous_revision != expected_revision
                or command.resulting_revision != expected_revision + 1
            ):
                raise ValueError("Reset 전 Command revision이 연속적이지 않습니다.")


JournalCommand = EditCommand | TransformCommand | ResetCommand


@dataclass(frozen=True)
class EditJournalCheckpoint:
    """Blender 표시 Transaction 실패 시 Journal 메타데이터를 복구할 지점입니다."""

    revision: int
    history: tuple[EditOperation | TransformOperation, ...]
    undo_stack: tuple[JournalCommand, ...]
    redo_stack: tuple[JournalCommand, ...]


def create_edit_command(operation: EditOperation, delta: EditDelta) -> EditCommand:
    """검증된 schema 2 operation과 Delta로 warm command를 생성합니다."""

    if not isinstance(operation, EditOperation):
        raise ValueError("Legacy schema 1 작업은 신규 warm Delta command로 만들 수 없습니다.")
    return EditCommand(
        operation=operation,
        delta=delta,
        previous_revision=operation.previous_revision,
        resulting_revision=operation.resulting_revision,
    )


def capture_edit_command(
    before_reconstruction: Reconstruction,
    before_indexes: RelationshipIndexes,
    after_reconstruction: Reconstruction,
    after_indexes: RelationshipIndexes,
    plan: RemovalPlan,
    operation: EditOperation,
) -> EditCommand:
    """외부 forward 처리의 전후 상태와 계획으로 warm command를 캡처합니다."""

    delta = capture_edit_delta(
        before_reconstruction,
        before_indexes,
        after_reconstruction,
        after_indexes,
        plan,
        operation,
    )
    return create_edit_command(operation, delta)


class EditJournal:
    """현재 적용 history와 독립적인 warm undo·redo stack을 소유합니다."""

    def __init__(
        self,
        dataset_uuid: str,
        source_fingerprint: str,
        *,
        initial_revision: int = 0,
    ) -> None:
        """빈 Journal을 지정 Source와 시작 revision 경계에 생성합니다."""

        initial_token = EditStateToken(
            dataset_uuid=dataset_uuid,
            source_fingerprint=source_fingerprint,
            edit_revision=initial_revision,
            index_revision=initial_revision,
        )
        self._dataset_uuid = initial_token.dataset_uuid
        self._source_fingerprint = initial_token.source_fingerprint
        self._revision = initial_revision
        self._history: list[EditOperation | TransformOperation] = []
        self._undo_stack: list[JournalCommand] = []
        self._redo_stack: list[JournalCommand] = []

    @property
    def dataset_uuid(self) -> str:
        """Journal이 고정한 Dataset UUID를 반환합니다."""

        return self._dataset_uuid

    @property
    def source_fingerprint(self) -> str:
        """Journal이 고정한 source fingerprint를 반환합니다."""

        return self._source_fingerprint

    @property
    def revision(self) -> int:
        """현재 적용 history와 일치하는 편집 revision을 반환합니다."""

        return self._revision

    @property
    def history(self) -> tuple[EditOperation | TransformOperation, ...]:
        """현재 Reconstruction에 적용된 저장 operation 목록을 반환합니다."""

        return tuple(self._history)

    @property
    def undo_count(self) -> int:
        """현재 barrier 이후 되돌릴 수 있는 command 수를 반환합니다."""

        return len(self._undo_stack)

    @property
    def redo_count(self) -> int:
        """현재 분기에서 다시 적용할 수 있는 command 수를 반환합니다."""

        return len(self._redo_stack)

    def poll_undo(self) -> bool:
        """빈 stack에서는 False인 Undo 실행 가능 상태를 반환합니다."""

        return bool(self._undo_stack)

    def poll_redo(self) -> bool:
        """빈 stack에서는 False인 Redo 실행 가능 상태를 반환합니다."""

        return bool(self._redo_stack)

    def can_undo(self) -> bool:
        """Undo poll의 명시적인 호환 별칭을 반환합니다."""

        return self.poll_undo()

    def can_redo(self) -> bool:
        """Redo poll의 명시적인 호환 별칭을 반환합니다."""

        return self.poll_redo()

    def _validate_command_for_push(self, command: EditCommand | TransformCommand) -> None:
        """새 command의 Source와 이전 revision이 현재 Journal 끝과 같은지 검사합니다."""

        if not isinstance(command, (EditCommand, TransformCommand)):
            raise TypeError("Journal에는 EditCommand만 추가할 수 있습니다.")
        if command.operation.dataset_uuid != self._dataset_uuid:
            raise ValueError("Journal command의 Dataset UUID가 현재 Journal과 다릅니다.")
        if command.operation.source_fingerprint != self._source_fingerprint:
            raise ValueError("Journal command의 source fingerprint가 현재 Journal과 다릅니다.")
        if command.previous_revision != self._revision:
            raise ValueError("Journal command의 이전 revision이 현재 Journal과 다릅니다.")
        dataset_transform_from_history((*self._history, command.operation))

    def push(self, command: EditCommand | TransformCommand) -> None:
        """이미 forward 적용된 새 command를 추가하고 기존 redo 분기를 폐기합니다."""

        self._validate_command_for_push(command)
        self._undo_stack.append(command)
        self._redo_stack.clear()
        self._history.append(command.operation)
        self._revision = command.resulting_revision

    def checkpoint(self) -> EditJournalCheckpoint:
        """현재 Stack과 History의 얕은 불변 Transaction 지점을 반환합니다."""

        return EditJournalCheckpoint(
            revision=self._revision,
            history=tuple(self._history),
            undo_stack=tuple(self._undo_stack),
            redo_stack=tuple(self._redo_stack),
        )

    def restore_checkpoint(self, checkpoint: EditJournalCheckpoint) -> None:
        """호출자가 Runtime 값을 복구한 뒤 Journal Stack 메타데이터를 되돌립니다."""

        if not isinstance(checkpoint, EditJournalCheckpoint):
            raise TypeError("Journal 복구 지점 형식이 올바르지 않습니다.")
        if checkpoint.revision != len(checkpoint.history):
            raise ValueError("Journal 복구 지점의 revision과 history가 다릅니다.")
        for operation in checkpoint.history:
            if (
                operation.dataset_uuid != self._dataset_uuid
                or operation.source_fingerprint != self._source_fingerprint
            ):
                raise ValueError("Journal 복구 지점의 Source token이 다릅니다.")
        self._revision = checkpoint.revision
        self._history = list(checkpoint.history)
        self._undo_stack = list(checkpoint.undo_stack)
        self._redo_stack = list(checkpoint.redo_stack)

    def append_rebuilt_command(self, command: EditCommand | TransformCommand) -> None:
        """외부 cold replay에서 재구축한 command를 순서대로 추가합니다."""

        self.push(command)

    def append_rebuilt(
        self,
        *,
        before_reconstruction: Reconstruction,
        before_indexes: RelationshipIndexes,
        after_reconstruction: Reconstruction,
        after_indexes: RelationshipIndexes,
        plan: RemovalPlan,
        operation: EditOperation,
    ) -> EditCommand:
        """외부 순차 replay 결과로 command를 캡처해 Journal에 추가합니다."""

        command = capture_edit_command(
            before_reconstruction,
            before_indexes,
            after_reconstruction,
            after_indexes,
            plan,
            operation,
        )
        self.push(command)
        return command

    def _validate_runtime_token(self, token: EditStateToken) -> None:
        """호출 Runtime token이 Journal Source와 현재 revision에 일치하는지 검사합니다."""

        if token.dataset_uuid != self._dataset_uuid:
            raise ValueError("Runtime token의 Dataset UUID가 현재 Journal과 다릅니다.")
        if token.source_fingerprint != self._source_fingerprint:
            raise ValueError("Runtime token의 source fingerprint가 현재 Journal과 다릅니다.")
        if token.edit_revision != token.index_revision:
            raise ValueError("Runtime 모델과 관계 인덱스 revision이 다릅니다.")
        if token.edit_revision != self._revision:
            raise ValueError("Runtime token revision이 현재 Journal과 다릅니다.")

    def undo(
        self,
        reconstruction: Reconstruction,
        indexes: RelationshipIndexes,
        token: EditStateToken,
    ) -> EditStateToken | None:
        """마지막 warm command를 역적용하고 history를 현재 상태에 맞춥니다."""

        if not self._undo_stack:
            return None
        self._validate_runtime_token(token)
        command = self._undo_stack[-1]
        if isinstance(command, ResetCommand):
            return self._undo_reset(reconstruction, indexes, token, command)
        if not self._history or self._history[-1] != command.operation:
            raise RuntimeError("Journal history와 Undo stack의 현재 끝이 다릅니다.")
        resulting_token = _apply_inverse(
            reconstruction,
            indexes,
            command,
            token,
        )
        self._undo_stack.pop()
        self._redo_stack.append(command)
        self._history.pop()
        self._revision = command.previous_revision
        return resulting_token

    def redo(
        self,
        reconstruction: Reconstruction,
        indexes: RelationshipIndexes,
        token: EditStateToken,
    ) -> EditStateToken | None:
        """마지막으로 되돌린 warm command를 재적용하고 history를 복구합니다."""

        if not self._redo_stack:
            return None
        self._validate_runtime_token(token)
        command = self._redo_stack[-1]
        if isinstance(command, ResetCommand):
            return self._redo_reset(reconstruction, indexes, token, command)
        if command.previous_revision != self._revision:
            raise RuntimeError("Journal Redo stack의 이전 revision이 현재 상태와 다릅니다.")
        resulting_token = _apply_forward(
            reconstruction,
            indexes,
            command,
            token,
        )
        self._redo_stack.pop()
        self._undo_stack.append(command)
        self._history.append(command.operation)
        self._revision = command.resulting_revision
        return resulting_token

    def reset(
        self,
        reconstruction: Reconstruction,
        indexes: RelationshipIndexes,
        token: EditStateToken,
    ) -> EditStateToken | None:
        """현재 확정 편집 전체를 역적용하고 되돌릴 수 있는 Reset Command를 기록합니다."""

        if not self._history:
            return None
        self._validate_runtime_token(token)
        if len(self._undo_stack) != len(self._history) or any(
            not isinstance(command, (EditCommand, TransformCommand)) for command in self._undo_stack
        ):
            raise RuntimeError("Reset 가능한 편집 Command 경계가 아닙니다.")
        commands = tuple(self._undo_stack)
        reset_command = ResetCommand(commands, self._revision)
        resulting_token = token
        applied: list[EditCommand | TransformCommand] = []
        try:
            for command in reversed(commands):
                resulting_token = _apply_inverse(
                    reconstruction,
                    indexes,
                    command,
                    resulting_token,
                )
                applied.append(command)
        except Exception:
            for command in reversed(applied):
                resulting_token = _apply_forward(
                    reconstruction,
                    indexes,
                    command,
                    resulting_token,
                )
            raise
        self._undo_stack = [reset_command]
        self._redo_stack.clear()
        self._history.clear()
        self._revision = 0
        return resulting_token

    def _undo_reset(
        self,
        reconstruction: Reconstruction,
        indexes: RelationshipIndexes,
        token: EditStateToken,
        command: ResetCommand,
    ) -> EditStateToken:
        """Reset을 Undo해 직전 편집 이력과 개별 Undo Command를 복원합니다."""

        if self._revision != 0 or self._history:
            raise RuntimeError("Reset Undo의 현재 Journal 경계가 올바르지 않습니다.")
        resulting_token = token
        applied: list[EditCommand | TransformCommand] = []
        try:
            for item in command.commands:
                resulting_token = _apply_forward(
                    reconstruction,
                    indexes,
                    item,
                    resulting_token,
                )
                applied.append(item)
        except Exception:
            for item in reversed(applied):
                resulting_token = _apply_inverse(
                    reconstruction,
                    indexes,
                    item,
                    resulting_token,
                )
            raise
        self._undo_stack.pop()
        self._undo_stack.extend(command.commands)
        self._redo_stack.append(command)
        self._history = [item.operation for item in command.commands]
        self._revision = command.previous_revision
        return resulting_token

    def _redo_reset(
        self,
        reconstruction: Reconstruction,
        indexes: RelationshipIndexes,
        token: EditStateToken,
        command: ResetCommand,
    ) -> EditStateToken:
        """Undo한 Reset을 다시 적용해 현재 편집 이력을 비웁니다."""

        command_count = len(command.commands)
        if (
            tuple(self._undo_stack[-command_count:]) != command.commands
            or tuple(self._history) != tuple(item.operation for item in command.commands)
        ):
            raise RuntimeError("Reset Redo의 현재 Journal 경계가 올바르지 않습니다.")
        resulting_token = token
        applied: list[EditCommand | TransformCommand] = []
        try:
            for item in reversed(command.commands):
                resulting_token = _apply_inverse(
                    reconstruction,
                    indexes,
                    item,
                    resulting_token,
                )
                applied.append(item)
        except Exception:
            for item in reversed(applied):
                resulting_token = _apply_forward(
                    reconstruction,
                    indexes,
                    item,
                    resulting_token,
                )
            raise
        del self._undo_stack[-command_count:]
        self._undo_stack.append(command)
        self._redo_stack.pop()
        self._history.clear()
        self._revision = 0
        return resulting_token

    def barrier(self) -> None:
        """현재 history는 유지하고 이전 command의 warm 이동 경계만 닫습니다."""

        self._undo_stack.clear()
        self._redo_stack.clear()

    def clear(
        self,
        *,
        dataset_uuid: str | None = None,
        source_fingerprint: str | None = None,
        revision: int = 0,
    ) -> None:
        """외부 Runtime 교체 경계에서 history와 두 stack을 완전히 초기화합니다."""

        next_token = EditStateToken(
            dataset_uuid=dataset_uuid or self._dataset_uuid,
            source_fingerprint=source_fingerprint or self._source_fingerprint,
            edit_revision=revision,
            index_revision=revision,
        )
        self._dataset_uuid = next_token.dataset_uuid
        self._source_fingerprint = next_token.source_fingerprint
        self._revision = revision
        self._history.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()


__all__ = [
    "EditCommand",
    "ResetCommand",
    "TransformCommand",
    "create_transform_command",
    "EditJournalCheckpoint",
    "EditJournal",
    "create_edit_command",
    "capture_edit_command",
]
