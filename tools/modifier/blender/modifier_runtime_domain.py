"""Blender Undo와 분리된 COLMAP Modifier Runtime 도메인을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from ..core.edit_replay import history_hash


def _pointer(value: Any) -> int:
    """유효한 Blender 객체의 현재 프로세스 포인터만 반환합니다."""

    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _normalized_uuid(value: str, label: str) -> str:
    """영속 식별자를 표준 소문자 UUID 문자열로 검증합니다."""

    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label}가 유효한 UUID가 아닙니다.") from error
    normalized = str(parsed)
    if normalized != str(value):
        raise ValueError(f"{label}는 표준 소문자 UUID 형식이어야 합니다.")
    return normalized


@dataclass(frozen=True)
class ModifierDomainIdentity:
    """Snapshot과 표시 재연결에 사용하는 복합 Runtime 식별자입니다."""

    domain_uuid: str
    domain_generation: int
    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    history_hash: str


@dataclass(frozen=True)
class ModifierDomainCheckpoint:
    """표시 Transaction 실패 시 독립 Runtime 도메인 메타데이터를 복구할 지점입니다."""

    dataset_state: Any
    journal: Any
    domain_generation: int
    persistence_dirty: bool
    saved_generation: int
    display_revision: int
    last_status: str
    frozen_reason: str


@dataclass
class ModifierRuntimeDomain:
    """Modifier 권위 데이터와 전용 작업 스택을 함께 소유합니다."""

    domain_uuid: str
    domain_generation: int
    dataset_state: Any
    journal: Any
    persistence_dirty: bool = False
    saved_generation: int = -1
    display_revision: int = 0
    session_generation: int = 0
    transient_scene_pointer: int = 0
    last_status: str = ""
    frozen_reason: str = ""

    def __post_init__(self) -> None:
        """도메인 식별자와 최초 권위 상태의 내부 토큰을 검증합니다."""

        self.domain_uuid = _normalized_uuid(self.domain_uuid, "Domain UUID")
        if type(self.domain_generation) is not int or self.domain_generation < 0:
            raise ValueError("Domain generation은 0 이상의 정수여야 합니다.")
        self.assert_integrity()

    @property
    def identity(self) -> ModifierDomainIdentity:
        """현재 권위 상태의 불변 복합 식별자를 반환합니다."""

        state = self.dataset_state
        return ModifierDomainIdentity(
            domain_uuid=self.domain_uuid,
            domain_generation=self.domain_generation,
            dataset_uuid=str(state.dataset_uuid),
            source_fingerprint=str(state.source.source_fingerprint),
            edit_revision=int(state.edit_revision),
            history_hash=str(state.history_hash),
        )

    @property
    def editable(self) -> bool:
        """도메인이 오류로 동결되지 않았는지 반환합니다."""

        return not bool(self.frozen_reason)

    def assert_integrity(self) -> None:
        """Runtime Reconstruction·Index·History 토큰의 일관성을 검사합니다."""

        state = self.dataset_state
        _normalized_uuid(str(state.dataset_uuid), "Dataset UUID")
        if state.edit_revision != state.index_revision:
            raise ValueError("Runtime 모델과 관계 인덱스 revision이 일치하지 않습니다.")
        if state.edit_revision != len(state.operations):
            raise ValueError("Runtime edit revision과 작업 이력 길이가 일치하지 않습니다.")
        if state.history_hash != history_hash(state.operations):
            raise ValueError("Runtime history hash가 작업 이력과 일치하지 않습니다.")
        for operation in state.operations:
            if operation.dataset_uuid != state.dataset_uuid:
                raise ValueError("작업 이력의 Dataset UUID가 Runtime과 다릅니다.")
            if operation.source_fingerprint != state.source.source_fingerprint:
                raise ValueError("작업 이력의 source fingerprint가 Runtime과 다릅니다.")
        journal = self.journal
        if journal is None:
            raise ValueError("Modifier Runtime Domain의 전용 Journal이 없습니다.")
        if (
            journal.dataset_uuid != state.dataset_uuid
            or journal.source_fingerprint != state.source.source_fingerprint
            or journal.revision != state.edit_revision
            or journal.history != state.operations
        ):
            raise ValueError("Modifier Runtime과 전용 Journal token이 일치하지 않습니다.")

    def require_editable(self) -> None:
        """동결된 도메인의 추가 편집을 fail-closed로 거부합니다."""

        if self.frozen_reason:
            raise RuntimeError(f"Modifier Runtime 편집이 동결되었습니다: {self.frozen_reason}")
        self.assert_integrity()

    def checkpoint(self) -> ModifierDomainCheckpoint:
        """현재 권위 상태와 저장·표시 메타데이터의 Transaction 지점을 반환합니다."""

        return ModifierDomainCheckpoint(
            dataset_state=self.dataset_state,
            journal=self.journal,
            domain_generation=self.domain_generation,
            persistence_dirty=self.persistence_dirty,
            saved_generation=self.saved_generation,
            display_revision=self.display_revision,
            last_status=self.last_status,
            frozen_reason=self.frozen_reason,
        )

    def restore_checkpoint(self, checkpoint: ModifierDomainCheckpoint) -> None:
        """호출자가 Runtime 값을 복구한 뒤 도메인 메타데이터를 원자 복구합니다."""

        if not isinstance(checkpoint, ModifierDomainCheckpoint):
            raise TypeError("Modifier Domain 복구 지점 형식이 올바르지 않습니다.")
        self.dataset_state = checkpoint.dataset_state
        self.journal = checkpoint.journal
        self.domain_generation = checkpoint.domain_generation
        self.persistence_dirty = checkpoint.persistence_dirty
        self.saved_generation = checkpoint.saved_generation
        self.display_revision = checkpoint.display_revision
        self.last_status = checkpoint.last_status
        self.frozen_reason = checkpoint.frozen_reason
        self.assert_integrity()

    def replace_state(
        self,
        state: Any,
        *,
        status: str,
        mark_dirty: bool,
        advance_generation: bool = True,
    ) -> None:
        """검증된 새 Dataset 상태를 원자적인 도메인 세대로 승격합니다."""

        previous = self.dataset_state
        if state.dataset_uuid != previous.dataset_uuid:
            raise ValueError("같은 Domain 안에서 Dataset UUID를 교체할 수 없습니다.")
        if state.source.source_fingerprint != previous.source.source_fingerprint:
            raise ValueError("같은 Domain 안에서 source fingerprint를 교체할 수 없습니다.")
        old_state = self.dataset_state
        self.dataset_state = state
        try:
            self.assert_integrity()
        except Exception:
            self.dataset_state = old_state
            raise
        if advance_generation:
            self.domain_generation += 1
        self.persistence_dirty = self.persistence_dirty or bool(mark_dirty)
        self.display_revision += 1
        self.last_status = str(status)

    def replace_baseline(
        self,
        state: Any,
        journal: Any,
        *,
        status: str,
        mark_dirty: bool,
    ) -> None:
        """Export 성공 결과와 빈 Journal을 같은 도메인의 새 Source 기준선으로 설치합니다."""

        if state.dataset_uuid != self.dataset_state.dataset_uuid:
            raise ValueError("같은 Domain 안에서 Dataset UUID를 교체할 수 없습니다.")
        previous_state = self.dataset_state
        previous_journal = self.journal
        self.dataset_state = state
        self.journal = journal
        try:
            self.assert_integrity()
            if (
                journal.dataset_uuid != state.dataset_uuid
                or journal.source_fingerprint != state.source.source_fingerprint
                or journal.revision != state.edit_revision
                or journal.history != state.operations
            ):
                raise ValueError("새 Baseline Journal이 Runtime 상태와 일치하지 않습니다.")
        except Exception:
            self.dataset_state = previous_state
            self.journal = previous_journal
            raise
        self.domain_generation += 1
        self.persistence_dirty = self.persistence_dirty or bool(mark_dirty)
        self.display_revision += 1
        self.last_status = str(status)

    def mark_saved(self, generation: int) -> None:
        """저장된 세대가 현재 세대일 때만 Dirty 상태를 해제합니다."""

        if generation != self.domain_generation:
            raise ValueError("저장된 Domain generation이 현재 Runtime과 다릅니다.")
        self.saved_generation = generation
        self.persistence_dirty = False

    def freeze(self, reason: str) -> None:
        """권위 데이터를 보존한 채 추가 편집만 중단합니다."""

        message = str(reason).strip()
        self.frozen_reason = message or "알 수 없는 Runtime 무결성 오류"
        self.last_status = self.frozen_reason

    def bind_scene(self, scene: Any) -> None:
        """Scene 포인터를 영속 식별자가 아닌 현재 세션 바인딩으로만 저장합니다."""

        pointer = _pointer(scene)
        if not pointer:
            raise ValueError("Modifier Domain에 연결할 유효한 Scene이 없습니다.")
        self.transient_scene_pointer = pointer
        self.session_generation += 1


_DOMAINS: dict[str, ModifierRuntimeDomain] = {}
_SCENE_BINDINGS: dict[int, str] = {}


def create_domain(
    state: Any,
    journal: Any,
    *,
    domain_uuid: str | None = None,
    domain_generation: int = 0,
    persistence_dirty: bool = False,
) -> ModifierRuntimeDomain:
    """검증된 Import 또는 복원 결과로 새 독립 Runtime 도메인을 만듭니다."""

    return ModifierRuntimeDomain(
        domain_uuid=domain_uuid or str(uuid.uuid4()),
        domain_generation=domain_generation,
        dataset_state=state,
        journal=journal,
        persistence_dirty=persistence_dirty,
    )


def install_domain(domain: ModifierRuntimeDomain, scene: Any | None = None) -> None:
    """완성된 도메인을 Registry에 설치하고 선택적으로 Scene에 연결합니다."""

    domain.assert_integrity()
    existing = _DOMAINS.get(domain.domain_uuid)
    if existing is not None and existing is not domain:
        raise ValueError("같은 Domain UUID의 다른 Runtime이 이미 설치되어 있습니다.")
    _DOMAINS[domain.domain_uuid] = domain
    if scene is not None:
        bind_domain(scene, domain)


def bind_domain(scene: Any, domain: ModifierRuntimeDomain) -> None:
    """현재 Scene을 설치된 도메인에 일시적으로 연결합니다."""

    if _DOMAINS.get(domain.domain_uuid) is not domain:
        raise ValueError("설치되지 않은 Modifier Domain은 Scene에 연결할 수 없습니다.")
    pointer = _pointer(scene)
    if not pointer:
        raise ValueError("Modifier Domain에 연결할 유효한 Scene이 없습니다.")
    previous_uuid = _SCENE_BINDINGS.get(pointer)
    if previous_uuid and previous_uuid != domain.domain_uuid:
        previous = _DOMAINS.get(previous_uuid)
        if previous is not None and previous.transient_scene_pointer == pointer:
            previous.transient_scene_pointer = 0
    _SCENE_BINDINGS[pointer] = domain.domain_uuid
    domain.bind_scene(scene)


def domain_by_uuid(domain_uuid: str) -> ModifierRuntimeDomain | None:
    """표준 Domain UUID로 현재 Runtime을 조회합니다."""

    return _DOMAINS.get(str(domain_uuid))


def domain_for_dataset(dataset_uuid: str) -> ModifierRuntimeDomain | None:
    """Dataset UUID가 유일하게 일치할 때만 Runtime 도메인을 반환합니다."""

    matches = tuple(
        domain
        for domain in _DOMAINS.values()
        if domain.dataset_state.dataset_uuid == str(dataset_uuid)
    )
    if len(matches) > 1:
        raise RuntimeError("같은 Dataset UUID의 Modifier Domain이 둘 이상입니다.")
    return matches[0] if matches else None


def domain_for_scene(scene: Any, *, required: bool = False) -> ModifierRuntimeDomain | None:
    """일시 Binding과 영속 Mirror를 순서대로 검사해 Scene 도메인을 찾습니다."""

    pointer = _pointer(scene)
    domain = _DOMAINS.get(_SCENE_BINDINGS.get(pointer, "")) if pointer else None
    if domain is None:
        settings = getattr(scene, "colmap_import", None)
        mirrored_uuid = str(getattr(settings, "domain_uuid", "") or "")
        domain = _DOMAINS.get(mirrored_uuid)
    if domain is None and len(_DOMAINS) == 1:
        candidate = next(iter(_DOMAINS.values()))
        settings = getattr(scene, "colmap_import", None)
        mirrored_dataset = str(getattr(settings, "dataset_uuid", "") or "")
        if not mirrored_dataset or candidate.dataset_state.dataset_uuid == mirrored_dataset:
            domain = candidate
    if domain is not None and pointer:
        bind_domain(scene, domain)
    if required and domain is None:
        raise ValueError("현재 Scene에 연결된 Modifier Runtime Domain이 없습니다.")
    return domain


def iter_domains() -> tuple[ModifierRuntimeDomain, ...]:
    """설치된 도메인의 안정적인 현재 Snapshot을 반환합니다."""

    return tuple(_DOMAINS.values())


def clear_transient_bindings() -> None:
    """Global Undo 전 Scene 포인터 연결만 폐기하고 권위 Runtime은 유지합니다."""

    _SCENE_BINDINGS.clear()
    for domain in _DOMAINS.values():
        domain.transient_scene_pointer = 0
        domain.session_generation += 1


def retire_domain(domain_uuid: str) -> None:
    """명시적으로 교체된 Domain과 연결 정보만 Registry에서 제거합니다."""

    normalized = str(domain_uuid)
    _DOMAINS.pop(normalized, None)
    stale = tuple(
        pointer
        for pointer, bound_uuid in _SCENE_BINDINGS.items()
        if bound_uuid == normalized
    )
    for pointer in stale:
        _SCENE_BINDINGS.pop(pointer, None)


def clear_domains() -> None:
    """파일 Load·Extension 해제 경계에서 모든 권위 Runtime을 폐기합니다."""

    _SCENE_BINDINGS.clear()
    _DOMAINS.clear()


__all__ = [
    "ModifierDomainCheckpoint",
    "ModifierDomainIdentity",
    "ModifierRuntimeDomain",
    "bind_domain",
    "clear_domains",
    "clear_transient_bindings",
    "create_domain",
    "domain_by_uuid",
    "domain_for_dataset",
    "domain_for_scene",
    "install_domain",
    "iter_domains",
    "retire_domain",
]
