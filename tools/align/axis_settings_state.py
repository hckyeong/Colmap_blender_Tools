"""형상별 축 편집의 확정 스냅샷과 저장하지 않는 임시 상태입니다."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from .display_axes import ModelDisplayTransform


@dataclass(frozen=True, slots=True)
class AxisGizmoPivotSnapshot:
    """기즈모의 선택 범위와 주 형상 원본 좌표를 작은 불변 값으로 보관합니다."""
    role: str = ""
    model_ids: tuple[str, ...] = ()
    source_model_id: str = ""
    source_position: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class AxisEditSnapshot:
    """형상별 축·기즈모와 해당 축에 의존하는 모델의 정렬 감사 상태를 함께 보관합니다."""
    model_id: str
    main_transform: ModelDisplayTransform
    companion_transform: ModelDisplayTransform | None
    provenance: Any
    related_provenances: tuple[tuple[str, Any], ...] = ()
    gizmo_pivot: AxisGizmoPivotSnapshot | None = None


@dataclass
class AxisSettingsRuntimeState:
    """하나의 팝업이 소유한 임시 축 설정과 편집 기준을 보관합니다."""
    token: str = ""
    model_id: str = ""
    main_transform: ModelDisplayTransform | None = None
    companion_transform: ModelDisplayTransform | None = None
    preview_enabled: bool = True
    baseline: AxisEditSnapshot | None = None
    baseline_alignment_fingerprint: str = ""
    geometry_identity: tuple = ()
    window_pointer: int = 0
    workspace_pointer: int = 0
    gizmo_pivot: AxisGizmoPivotSnapshot | None = None

    @property
    def is_active(self) -> bool:
        """유효한 편집 토큰이 있는 동안만 임시 설정을 활성 상태로 판정합니다."""
        return bool(self.token)

    def clear(self) -> None:
        """팝업 종료·취소·파일 교체에서 임시 축·기즈모와 기준 참조를 제거합니다."""
        self.token = ""
        self.model_id = ""
        self.main_transform = None
        self.companion_transform = None
        self.preview_enabled = True
        self.baseline = None
        self.baseline_alignment_fingerprint = ""
        self.geometry_identity = ()
        self.window_pointer = 0
        self.workspace_pointer = 0
        self.gizmo_pivot = None
