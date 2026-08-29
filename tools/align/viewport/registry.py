"""단일 메인 창의 듀얼 뷰를 현재 Blender 화면에서 재탐색합니다."""

from __future__ import annotations

from typing import Any, Iterable

try:
    from ....shared.blender_runtime.workspace_factory import (
        screen_matches_workspace,
        workspace_matches_spec,
    )
except ImportError:  # Source Tree를 최상위 tools 패키지로 실행하는 기존 테스트를 지원합니다.
    from shared.blender_runtime.workspace_factory import (
        screen_matches_workspace,
        workspace_matches_spec,
    )

from ..constants import (
    MAIN_SCREEN_OWNER_KEY,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from ..state import RUNTIME
from ..workspace_contract import ALIGN_WORKSPACE_SPEC


_VALID_AREA_ROLES = frozenset({ROLE_REFERENCE, ROLE_MOVING})


def find_window_region(area: Any) -> Any | None:
    """Area 안에서 실제 편집 화면인 WINDOW Region을 찾습니다."""
    return next((region for region in area.regions if region.type == "WINDOW"), None)


def classify_areas(areas: Iterable[Any]) -> tuple[Any, Any] | None:
    """메인 창의 정확히 두 VIEW_3D Area를 왼쪽 A와 오른쪽 P로 분류합니다."""
    area_list = list(areas)
    if len(area_list) != 2 or any(area.type != "VIEW_3D" for area in area_list):
        return None
    view_areas = sorted(area_list, key=lambda area: area.x)
    if any(find_window_region(area) is None for area in view_areas):
        return None
    return view_areas[0], view_areas[1]


def refresh_registry(window: Any) -> bool:
    """메인 Window의 실제 듀얼 뷰를 검사해 역할 포인터를 새로 기록합니다."""
    registry = RUNTIME.viewport_registry
    if (
        window is None
        or not workspace_matches_spec(
            window.workspace,
            ALIGN_WORKSPACE_SPEC,
            require_ready=False,
        )
        or not screen_matches_workspace(window.screen, window.workspace)
        or not window.screen.get(MAIN_SCREEN_OWNER_KEY, False)
    ):
        registry.workspace_name = ""
        registry.main_window_pointer = 0
        registry.reference_area_pointer = 0
        registry.moving_area_pointer = 0
        registry.reference_region_pointer = 0
        registry.moving_region_pointer = 0
        return False
    classified = classify_areas(window.screen.areas)
    if classified is None:
        registry.workspace_name = ""
        registry.main_window_pointer = 0
        registry.reference_area_pointer = 0
        registry.moving_area_pointer = 0
        registry.reference_region_pointer = 0
        registry.moving_region_pointer = 0
        return False
    reference_area, moving_area = classified
    reference_region = find_window_region(reference_area)
    moving_region = find_window_region(moving_area)
    registry.workspace_name = window.workspace.name
    registry.main_window_pointer = window.as_pointer()
    registry.reference_area_pointer = reference_area.as_pointer()
    registry.moving_area_pointer = moving_area.as_pointer()
    registry.reference_region_pointer = reference_region.as_pointer()
    registry.moving_region_pointer = moving_region.as_pointer()
    RUNTIME.main_window_ref = window
    return True


def invalidate_registry() -> None:
    """Window·Screen·Area 변경 뒤 모든 런타임 포인터를 무효화합니다."""
    RUNTIME.viewport_registry.clear()


def _area_by_pointer(window: Any, pointer: int) -> Any | None:
    """현재 Screen에서 주어진 런타임 포인터와 같은 Area를 찾습니다."""
    return next((area for area in window.screen.areas if area.as_pointer() == pointer), None)


def resolve_area(window: Any, role: str) -> Any | None:
    """허용된 A/P Area 역할에 맞는 현재 Area만 반환합니다."""
    registry = RUNTIME.viewport_registry
    if window is None or role not in _VALID_AREA_ROLES:
        return None
    if (
        window.as_pointer() != registry.main_window_pointer
        or not registry.is_main_complete()
    ) and not refresh_registry(window):
        return None
    pointer = (
        registry.reference_area_pointer
        if role == ROLE_REFERENCE
        else registry.moving_area_pointer
    )
    area = _area_by_pointer(window, pointer) if pointer else None
    if area is None or area.type != "VIEW_3D":
        if not refresh_registry(window):
            return None
        pointer = (
            registry.reference_area_pointer
            if role == ROLE_REFERENCE
            else registry.moving_area_pointer
        )
        area = _area_by_pointer(window, pointer)
    return area


def role_for_area(window: Any, area: Any) -> str | None:
    """메인 창의 현재 Area 포인터가 기준 또는 이동 역할인지 판별합니다."""
    if window is None or area is None:
        return None
    registry = RUNTIME.viewport_registry
    if (
        window.as_pointer() != registry.main_window_pointer
        or not registry.is_main_complete()
    ) and not refresh_registry(window):
        return None
    pointer = area.as_pointer()
    if pointer == registry.reference_area_pointer:
        return ROLE_REFERENCE
    if pointer == registry.moving_area_pointer:
        return ROLE_MOVING
    return None
