"""Scene Collection과 무관한 듀얼 뷰 Area 역할 등록을 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
import sys
from pathlib import Path


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


@dataclass
class FakeRegion:
    """Area 분류 테스트에 필요한 최소 Region 정보를 제공합니다."""

    type: str
    pointer: int
    x: int = 0
    y: int = 0
    width: int = 400
    height: int = 400

    def as_pointer(self) -> int:
        """Blender RNA 객체와 같은 방식으로 런타임 포인터를 반환합니다."""
        return self.pointer


@dataclass
class FakeArea:
    """Area 분류 테스트에 필요한 최소 Editor Area 정보를 제공합니다."""

    type: str
    x: int
    pointer: int
    regions: tuple[FakeRegion, ...]
    y: int = 0
    width: int = 400
    height: int = 400

    def as_pointer(self) -> int:
        """Blender RNA 객체와 같은 방식으로 런타임 포인터를 반환합니다."""
        return self.pointer


@dataclass
class FakeScreen:
    """테스트용 Area와 애드온 소유 표시를 보관합니다."""

    areas: tuple[FakeArea, ...]
    pointer: int
    properties: dict[str, object] = field(default_factory=dict)

    def as_pointer(self) -> int:
        """Blender Screen처럼 런타임 포인터를 반환합니다."""
        return self.pointer

    def get(self, key: str, default=None):
        """Blender ID 사용자 속성과 같은 방식으로 값을 반환합니다."""
        return self.properties.get(key, default)


@dataclass
class FakeWorkspace:
    """테스트용 작업공간의 포인터·소유 메타데이터·Screen을 보관합니다."""

    name: str
    pointer: int = 0
    properties: dict[str, object] = field(default_factory=dict)
    screens: tuple[FakeScreen, ...] = ()

    def as_pointer(self) -> int:
        """Blender Workspace처럼 런타임 포인터를 반환합니다."""
        return self.pointer

    def get(self, key: str, default=None):
        """Blender ID 사용자 속성과 같은 방식으로 값을 반환합니다."""
        return self.properties.get(key, default)


@dataclass
class FakeWindow:
    """레지스트리 갱신에 필요한 Workspace와 Screen을 제공합니다."""

    workspace: FakeWorkspace
    screen: FakeScreen
    pointer: int

    def as_pointer(self) -> int:
        """Blender Window처럼 런타임 포인터를 반환합니다."""
        return self.pointer


def _make_main_areas() -> tuple[FakeArea, FakeArea]:
    """왼쪽 A와 오른쪽 P의 유효한 메인 Area 구성을 만듭니다."""
    reference = FakeArea("VIEW_3D", 0, 101, (FakeRegion("WINDOW", 201),))
    moving = FakeArea("VIEW_3D", 400, 102, (FakeRegion("WINDOW", 202),))
    return reference, moving


def _make_owned_window(screen: FakeScreen, window_pointer: int) -> FakeWindow:
    """현행 Align UUID·Schema가 연결된 Workspace–Screen 테스트 창을 만듭니다."""
    from shared.blender_runtime.workspace_factory import (
        SCREEN_OWNER_KEY,
        STATE_READY,
        WORKSPACE_OWNER_KEY,
        WORKSPACE_SCHEMA_KEY,
        WORKSPACE_SEED_SCHEMA_KEY,
        WORKSPACE_STATE_KEY,
        WORKSPACE_TOOL_ID_KEY,
        WORKSPACE_UUID_KEY,
    )
    from tools.align.constants import MAIN_SCREEN_OWNER_KEY, WORKSPACE_NAME

    workspace_uuid = f"align-test-{window_pointer}"
    screen.properties.update(
        {
            MAIN_SCREEN_OWNER_KEY: True,
            SCREEN_OWNER_KEY: workspace_uuid,
        }
    )
    workspace = FakeWorkspace(
        WORKSPACE_NAME,
        pointer=window_pointer + 1_000,
        properties={
            WORKSPACE_OWNER_KEY: "colmap_tools",
            WORKSPACE_TOOL_ID_KEY: "align",
            WORKSPACE_UUID_KEY: workspace_uuid,
            WORKSPACE_SCHEMA_KEY: 2,
            WORKSPACE_SEED_SCHEMA_KEY: 1,
            WORKSPACE_STATE_KEY: STATE_READY,
        },
        screens=(screen,),
    )
    return FakeWindow(workspace, screen, window_pointer)


def test_classify_areas_validates_two_view_layout() -> None:
    """메인 창은 정확히 두 VIEW_3D Area만 허용하는지 확인합니다."""
    from tools.align.viewport.registry import classify_areas

    reference, moving = _make_main_areas()
    assert classify_areas((moving, reference)) == (reference, moving)
    assert classify_areas((reference,)) is None
    assert classify_areas(
        (reference, FakeArea("PROPERTIES", 400, 102, (FakeRegion("WINDOW", 202),)))
    ) is None


def test_refresh_registry_records_main_window() -> None:
    """메인 Window의 A/P Area·Region 포인터와 역할만으로 등록하는지 확인합니다."""
    from tools.align.constants import (
        MAIN_SCREEN_OWNER_KEY,
        ROLE_MOVING,
        ROLE_REFERENCE,
        WORKSPACE_NAME,
    )
    from tools.align.state import RUNTIME
    from tools.align.viewport.registry import refresh_registry, role_for_area

    reference, moving = _make_main_areas()
    invalid_window = FakeWindow(
        FakeWorkspace("기본 작업공간"),
        FakeScreen((reference, moving), 301),
        401,
    )
    assert refresh_registry(invalid_window) is False
    assert RUNTIME.viewport_registry.is_main_complete() is False
    unowned_window = FakeWindow(
        FakeWorkspace(WORKSPACE_NAME),
        FakeScreen((reference, moving), 304),
        404,
    )
    assert refresh_registry(unowned_window) is False
    assert RUNTIME.viewport_registry.is_main_complete() is False

    main_window = _make_owned_window(FakeScreen((reference, moving), 302), 402)
    assert refresh_registry(main_window) is True
    registry = RUNTIME.viewport_registry
    assert registry.is_main_complete() is True
    assert registry.main_window_pointer == 402
    assert registry.reference_area_pointer == 101
    assert registry.moving_area_pointer == 102

    assert registry.is_complete() is True
    assert role_for_area(main_window, reference) == ROLE_REFERENCE
    assert role_for_area(main_window, moving) == ROLE_MOVING


def test_viewport_mouse_lookup_rejects_overlapping_ui_regions() -> None:
    """WINDOW 위에 겹친 N 패널·헤더 좌표를 역할 뷰포트로 오인하지 않는지 확인합니다."""

    from tools.align.constants import MAIN_SCREEN_OWNER_KEY, ROLE_MOVING, WORKSPACE_NAME
    from tools.align.viewport.picking import find_viewport_under_mouse
    from tools.align.viewport.registry import refresh_registry

    reference = FakeArea(
        "VIEW_3D",
        0,
        111,
        (FakeRegion("WINDOW", 211, 0, 0, 400, 400),),
    )
    moving_window = FakeRegion("WINDOW", 212, 400, 0, 400, 400)
    moving_ui = FakeRegion("UI", 213, 650, 0, 150, 400)
    moving_header = FakeRegion("HEADER", 214, 400, 374, 400, 26)
    moving = FakeArea(
        "VIEW_3D",
        400,
        112,
        (moving_window, moving_ui, moving_header),
    )
    window = _make_owned_window(FakeScreen((reference, moving), 312), 412)
    assert refresh_registry(window) is True
    assert find_viewport_under_mouse(window, 500, 200) == (
        moving,
        moving_window,
        ROLE_MOVING,
    )
    assert find_viewport_under_mouse(window, 700, 200) is None
    assert find_viewport_under_mouse(window, 500, 390) is None


def run_all() -> None:
    """이 파일의 단일 메인 창 분류와 레지스트리 검사를 실행합니다."""
    _add_source_path()
    test_classify_areas_validates_two_view_layout()
    test_refresh_registry_records_main_window()
    test_viewport_mouse_lookup_rejects_overlapping_ui_regions()
    print("test_viewport_registry: 통과")


if __name__ == "__main__":
    run_all()
