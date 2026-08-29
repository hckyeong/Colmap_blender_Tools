"""Data Layers 모델 ID 렌더 필터와 기존 듀얼 Area 경계를 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _demo_catalog():
    """제품의 빈 기본값과 분리해 테스트용 Demo 카탈로그를 명시적으로 만듭니다."""
    from tools.align.state import ModelCatalogState

    catalog = ModelCatalogState()
    catalog.reset_demo_catalog()
    return catalog


@dataclass
class _FakeRegion:
    """Area 역할 분류에 필요한 최소 WINDOW Region을 제공합니다."""

    type: str
    pointer: int

    def as_pointer(self) -> int:
        """Blender RNA 객체와 같은 런타임 포인터를 반환합니다."""
        return self.pointer


@dataclass
class _FakeArea:
    """듀얼 VIEW_3D 역할 분류를 위한 최소 Area 정보를 제공합니다."""

    type: str
    x: int
    pointer: int
    regions: tuple[_FakeRegion, ...]

    def as_pointer(self) -> int:
        """Blender RNA 객체와 같은 런타임 포인터를 반환합니다."""
        return self.pointer


@dataclass
class _FakeScreen:
    """테스트용 Area 집합과 애드온 소유 Screen 표식을 보관합니다."""

    areas: tuple[_FakeArea, ...]
    pointer: int
    properties: dict[str, object]

    def as_pointer(self) -> int:
        """Blender Screen과 같은 런타임 포인터를 반환합니다."""
        return self.pointer

    def get(self, key: str, default=None):
        """Blender IDProperty 조회와 같은 방식으로 값을 반환합니다."""
        return self.properties.get(key, default)


@dataclass
class _FakeWorkspace:
    """레지스트리 갱신에 필요한 작업공간 소유 메탁정보를 보관합니다."""

    name: str
    pointer: int = 0
    properties: dict[str, object] | None = None
    screens: tuple[_FakeScreen, ...] = ()

    def as_pointer(self) -> int:
        """Blender Workspace처럼 고정 포인터를 반환합니다."""
        return self.pointer

    def get(self, key: str, default=None):
        """Blender ID 사용자 속성과 같은 방식으로 값을 반환합니다."""
        return (self.properties or {}).get(key, default)


@dataclass
class _FakeWindow:
    """레지스트리 갱신에 필요한 작업공간과 Screen을 제공합니다."""

    workspace: _FakeWorkspace
    screen: _FakeScreen
    pointer: int

    def as_pointer(self) -> int:
        """Blender Window와 같은 런타임 포인터를 반환합니다."""
        return self.pointer


def _make_owned_window(areas: tuple[object, ...], screen_pointer: int, window_pointer: int) -> _FakeWindow:
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
    screen = _FakeScreen(
        areas,
        screen_pointer,
        {
            MAIN_SCREEN_OWNER_KEY: True,
            SCREEN_OWNER_KEY: workspace_uuid,
        },
    )
    workspace = _FakeWorkspace(
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
    return _FakeWindow(workspace, screen, window_pointer)


def _make_view_areas(count: int) -> tuple[_FakeArea, ...]:
    """x좌표가 증가하는 지정 개수의 VIEW_3D Area를 생성합니다."""
    return tuple(
        _FakeArea(
            type="VIEW_3D",
            x=index * 400,
            pointer=100 + index,
            regions=(_FakeRegion("WINDOW", 200 + index),),
        )
        for index in range(count)
    )


def test_demo_factory_uses_four_model_ids_without_scene_data() -> None:
    """공개 데모 생성기가 네 모델 ID 점군만 만들고 Scene 데이터는 만들지 않는지 확인합니다."""
    from tools.align.constants import DEMO_MODEL_IDS
    from tools.align.operators.demo_data import generate_demo_point_clouds

    clouds = generate_demo_point_clouds(point_count=17)
    assert tuple(clouds) == DEMO_MODEL_IDS
    assert len(clouds) == 4
    assert tuple(cloud.model_id for cloud in clouds.values()) == DEMO_MODEL_IDS
    assert all(len(cloud.positions) == 17 for cloud in clouds.values())


def test_render_filter_submits_active_left_and_one_selected_inactive_right() -> None:
    """렌더 필터가 활성 전체·선택 비활성 하나·알 수 없는 역할 빈 결과를 반환하는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.state import ModelCatalogState
    from tools.align.viewport.renderer import render_model_ids_for_role

    catalog = _demo_catalog()
    available = {DEMO_MODEL_1_ID, DEMO_MODEL_2_ID, DEMO_MODEL_3_ID, DEMO_MODEL_4_ID}
    assert render_model_ids_for_role(ROLE_REFERENCE, catalog, available) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
    )
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == ()
    assert catalog.select_model(
        DEMO_MODEL_3_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert render_model_ids_for_role(ROLE_REFERENCE, catalog, available) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
    )
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == (DEMO_MODEL_3_ID,)
    assert DEMO_MODEL_4_ID not in render_model_ids_for_role(ROLE_MOVING, catalog, available)
    assert render_model_ids_for_role("UNKNOWN", catalog, available) == ()
    assert render_model_ids_for_role(ROLE_MOVING, catalog, {DEMO_MODEL_4_ID}) == ()
    assert catalog.toggle_model_visibility(DEMO_MODEL_1_ID)
    assert catalog.toggle_model_visibility(DEMO_MODEL_3_ID)
    assert render_model_ids_for_role(ROLE_REFERENCE, catalog, available) == (DEMO_MODEL_2_ID,)
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == ()


def test_modifier_multiselection_returns_every_visible_model_for_combined_view() -> None:
    """수정 키로 선택한 A/P 전체가 개수 제한 없이 결합 표시 대상이 되는지 확인합니다."""

    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.viewport.renderer import multiselection_model_ids_for_role

    catalog = _demo_catalog()
    available = set(catalog.model_order)
    assert catalog.select_model(
        DEMO_MODEL_3_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.toggle_alignment_selection(
        DEMO_MODEL_2_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.toggle_alignment_selection(
        DEMO_MODEL_4_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.toggle_alignment_selection(
        DEMO_MODEL_1_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.selected_model_ids == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )
    assert multiselection_model_ids_for_role(
        ROLE_REFERENCE,
        catalog,
        available,
    ) == (DEMO_MODEL_1_ID, DEMO_MODEL_2_ID)
    assert multiselection_model_ids_for_role(
        ROLE_MOVING,
        catalog,
        available,
    ) == (DEMO_MODEL_3_ID, DEMO_MODEL_4_ID)
    assert multiselection_model_ids_for_role("UNKNOWN", catalog, available) == ()

    assert catalog.toggle_model_visibility(DEMO_MODEL_2_ID)
    assert multiselection_model_ids_for_role(ROLE_REFERENCE, catalog, available) == (
        DEMO_MODEL_1_ID,
    )


def test_combined_draw_passes_show_more_than_two_selected_layers_in_both_views() -> None:
    """세 개 이상 선택해도 A/P 양쪽 뷰가 선택된 전체 점군을 같은 순서로 합성하는지 확인합니다."""

    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.state import RUNTIME
    from tools.align.viewport.renderer import point_cloud_draw_passes_for_role

    catalog = _demo_catalog()
    assert catalog.select_model(
        DEMO_MODEL_3_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    for model_id in (DEMO_MODEL_2_ID, DEMO_MODEL_4_ID, DEMO_MODEL_1_ID):
        assert catalog.toggle_alignment_selection(
            model_id,
            has_marker_draft=False,
            completed_pair_count=0,
        )

    original_catalog = RUNTIME.model_catalog
    original_repository = RUNTIME.model_repository
    original_clouds = RUNTIME.point_clouds
    try:
        RUNTIME.model_catalog = catalog
        RUNTIME.model_repository = SimpleNamespace(models={})
        RUNTIME.point_clouds = {model_id: object() for model_id in catalog.model_order}
        expected = (
            (ROLE_REFERENCE, (DEMO_MODEL_1_ID, DEMO_MODEL_2_ID)),
            (ROLE_MOVING, (DEMO_MODEL_3_ID, DEMO_MODEL_4_ID)),
        )
        for role in (ROLE_REFERENCE, ROLE_MOVING):
            passes = point_cloud_draw_passes_for_role(role)
            assert tuple((draw_pass.role, draw_pass.model_ids) for draw_pass in passes) == expected
    finally:
        RUNTIME.model_catalog = original_catalog
        RUNTIME.model_repository = original_repository
        RUNTIME.point_clouds = original_clouds


def test_group_transfer_moves_selected_layer_between_right_and_left_filters() -> None:
    """양방향 그룹 전환이 선택을 유지하며 레이어를 오른쪽과 왼쪽 사이로 옮기는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        MODEL_ACTIVATION_SOURCE_MANUAL,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.state import ModelCatalogState
    from tools.align.viewport.renderer import render_model_ids_for_role

    catalog = _demo_catalog()
    available = {DEMO_MODEL_1_ID, DEMO_MODEL_2_ID, DEMO_MODEL_3_ID, DEMO_MODEL_4_ID}
    assert catalog.select_model(
        DEMO_MODEL_3_ID,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == (DEMO_MODEL_3_ID,)
    assert catalog.transfer_model_group(
        DEMO_MODEL_3_ID,
        source=MODEL_ACTIVATION_SOURCE_MANUAL,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == (DEMO_MODEL_4_ID,)
    assert render_model_ids_for_role(ROLE_REFERENCE, catalog, available) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
    )
    assert catalog.transfer_model_group(
        DEMO_MODEL_3_ID,
        source=MODEL_ACTIVATION_SOURCE_MANUAL,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == (DEMO_MODEL_3_ID,)
    assert render_model_ids_for_role(ROLE_REFERENCE, catalog, available) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
    )


def test_registry_keeps_exactly_two_areas_and_rejects_unknown_role() -> None:
    """모델 수와 무관하게 Area 레지스트리가 듀얼 뷰와 알 수 없는 역할 거부를 유지하는지 확인합니다."""
    from tools.align.constants import MAIN_SCREEN_OWNER_KEY, WORKSPACE_NAME
    from tools.align.state import RUNTIME
    from tools.align.viewport.registry import classify_areas, refresh_registry, resolve_area, role_for_area

    try:
        for count in (3, 4):
            areas = _make_view_areas(count)
            assert classify_areas(areas) is None
            window = _make_owned_window(areas, 300 + count, 400 + count)
            assert refresh_registry(window) is False
            assert RUNTIME.viewport_registry.is_main_complete() is False
        reference_area, moving_area = _make_view_areas(2)
        window = _make_owned_window((reference_area, moving_area), 503, 603)
        assert refresh_registry(window) is True
        assert resolve_area(window, "demo_3") is None
        assert role_for_area(window, moving_area) == "MOVING"
    finally:
        RUNTIME.viewport_registry.clear()
        RUNTIME.main_window_ref = None


def test_workspace_adjacency_accepts_scaled_five_pixel_border() -> None:
    """고배율 UI의 5픽셀 Area 경계만 조인 가능한 인접 영역으로 판정하는지 확인합니다."""
    from tools.align.operators.workspace import _areas_are_adjacent

    upper = SimpleNamespace(x=3, y=35, width=3150, height=143)
    scaled_border = SimpleNamespace(x=3, y=183, width=3150, height=1832)
    wider_gap = SimpleNamespace(x=3, y=184, width=3150, height=1832)
    misaligned_border = SimpleNamespace(x=20, y=183, width=3133, height=1832)

    assert _areas_are_adjacent(upper, scaled_border) is True
    assert _areas_are_adjacent(upper, wider_gap) is False
    assert _areas_are_adjacent(upper, misaligned_border) is False


def test_workspace_collapse_uses_blender_topology_when_rna_coordinates_are_stale() -> None:
    """RNA 좌표가 어긋난 결합 후 Area도 Blender 내부 위상 후보로 다시 시도하는지 확인합니다."""
    from tools.align.constants import MAIN_SCREEN_OWNER_KEY
    from tools.align.operators import workspace as workspace_module

    left = SimpleNamespace(type="VIEW_3D", x=3, y=183, width=3150, height=1832)
    right = SimpleNamespace(type="PROPERTIES", x=3158, y=35, width=679, height=1980)
    window = _make_owned_window((left, right), 703, 803)
    screen = window.screen
    attempted_pairs = []
    original_join = workspace_module._join_area_pair
    try:
        workspace_module._join_area_pair = (
            lambda _context, _window, source, target: (
                attempted_pairs.append((source, target)) or True
            )
        )
        assert workspace_module._next_joinable_pair(screen.areas) is None
        assert workspace_module._collapse_one_area(SimpleNamespace(), window) is True
        assert attempted_pairs
    finally:
        workspace_module._join_area_pair = original_join


def test_marker_state_rejects_extra_model_role() -> None:
    """마커 드래프트가 A/P 이외 모델 역할을 확정 후보로 받지 않는지 확인합니다."""
    from tools.align.state import MarkerSelectionState

    state = MarkerSelectionState()
    assert state.begin_create_draft() is True
    assert state.select_draft_point("demo_3", 0, (0.0, 0.0, 0.0)) is False
    assert state.draft is not None
    assert state.draft.reference is None
    assert state.draft.moving is None
    assert state.message == "마커 역할이 올바르지 않습니다."


def test_reference_picker_compares_all_visible_a_models() -> None:
    """A 피커가 Anchor 한 개가 아니라 표시 중인 활성 모델 전체 후보를 비교하는지 확인합니다."""
    from mathutils import Matrix

    from tools.align.alignment import identity_sim3_transform
    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_2_ID, ROLE_REFERENCE
    from tools.align.model_repository import ColmapModelRepository
    from tools.align.state import RUNTIME
    from tools.align.viewport import picking

    original_catalog = RUNTIME.model_catalog
    original_repository = RUNTIME.model_repository
    original_clouds = RUNTIME.point_clouds
    try:
        RUNTIME.model_catalog = _demo_catalog()
        RUNTIME.model_repository = ColmapModelRepository()
        # point_clouds fixture도 실제 경로와 동일하게 확정 identity alignment를 명시합니다.
        RUNTIME.model_repository.alignment_transforms = {
            DEMO_MODEL_1_ID: identity_sim3_transform(),
            DEMO_MODEL_2_ID: identity_sim3_transform(),
        }
        RUNTIME.point_clouds = {
            DEMO_MODEL_1_ID: SimpleNamespace(
                point_ids=(101,),
                positions=((6.0, 0.0, -1.0),),
            ),
            DEMO_MODEL_2_ID: SimpleNamespace(
                point_ids=(202,),
                positions=((1.0, 0.0, -2.0),),
            ),
        }
        region_3d = SimpleNamespace(view_matrix=Matrix.Identity(4))
        assert picking.marker_source_model_ids(ROLE_REFERENCE) == (
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
        )
        result = picking.pick_nearest_point(
            ROLE_REFERENCE,
            SimpleNamespace(),
            region_3d,
            (0.0, 0.0),
            radius=10.0,
        )
        assert result is not None
        assert result.model_id == DEMO_MODEL_2_ID
        assert result.point3d_id == 202

        assert RUNTIME.model_catalog.toggle_model_visibility(DEMO_MODEL_2_ID)
        assert picking.marker_source_model_ids(ROLE_REFERENCE) == (DEMO_MODEL_1_ID,)
        hidden_result = picking.pick_nearest_point(
            ROLE_REFERENCE,
            SimpleNamespace(),
            region_3d,
            (0.0, 0.0),
            radius=10.0,
        )
        assert hidden_result is not None
        assert hidden_result.model_id == DEMO_MODEL_1_ID
    finally:
        RUNTIME.model_catalog = original_catalog
        RUNTIME.model_repository = original_repository
        RUNTIME.point_clouds = original_clouds


def run_all() -> None:
    """Data Layers 렌더·레지스트리 경계를 순서대로 검증합니다."""
    _add_source_path()
    test_demo_factory_uses_four_model_ids_without_scene_data()
    test_render_filter_submits_active_left_and_one_selected_inactive_right()
    test_modifier_multiselection_returns_every_visible_model_for_combined_view()
    test_combined_draw_passes_show_more_than_two_selected_layers_in_both_views()
    test_group_transfer_moves_selected_layer_between_right_and_left_filters()
    test_registry_keeps_exactly_two_areas_and_rejects_unknown_role()
    test_workspace_adjacency_accepts_scaled_five_pixel_border()
    test_workspace_collapse_uses_blender_topology_when_rna_coordinates_are_stale()
    test_marker_state_rejects_extra_model_role()
    test_reference_picker_compares_all_visible_a_models()
    print("test_multi_model_limitations: 통과")


if __name__ == "__main__":
    run_all()
