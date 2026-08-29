"""A/P GPU 표시 집합과 Frame All 진입점의 단위 계약을 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from mathutils import Matrix, Quaternion, Vector


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def test_fit_role_view_uses_render_model_ids_for_default_targets() -> None:
    """기본 대상은 실제 렌더러 표시 집합이고 명시적 ID는 그대로 유지되는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.operators import workspace

    calls: list[tuple[str, ...]] = []
    original_resolve = workspace.resolve_area
    original_render_ids = workspace.render_model_ids_for_role
    original_bounds = workspace._bounds_for_model_ids
    original_fit = workspace._fit_area_to_bounds
    try:
        workspace.resolve_area = lambda _window, _role: object()
        workspace.render_model_ids_for_role = lambda _role: ("p-1", "p-2")
        workspace._bounds_for_model_ids = lambda ids: (
            calls.append(tuple(ids)) or (Vector((0.0, 0.0, 0.0)), Vector((1.0, 1.0, 1.0)))
        )
        workspace._fit_area_to_bounds = lambda _area, _bounds: None

        assert workspace.fit_role_view(object(), ROLE_MOVING) is True
        assert calls == [("p-1", "p-2")]
        calls.clear()
        assert workspace.fit_role_view(object(), ROLE_MOVING, model_ids=("imported",)) is True
        assert calls == [("imported",)]
    finally:
        workspace.resolve_area = original_resolve
        workspace.render_model_ids_for_role = original_render_ids
        workspace._bounds_for_model_ids = original_bounds
        workspace._fit_area_to_bounds = original_fit


def test_render_model_ids_match_visible_a_and_selected_visible_p_subset() -> None:
    """렌더러의 A 전체·P 선택 subset이 숨김·비선택 레이어를 제외하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.viewport.renderer import render_model_ids_for_role

    records = {
        model_id: SimpleNamespace(model_id=model_id, is_visible=is_visible)
        for model_id, is_visible in (
            ("a-visible", True),
            ("a-hidden", False),
            ("p-visible", True),
            ("p-hidden", False),
            ("p-unselected", True),
        )
    }
    catalog = SimpleNamespace(
        models=records,
        active_models=lambda: (records["a-visible"], records["a-hidden"]),
        selected_moving_model_ids=("p-visible", "p-hidden"),
    )
    available = set(records)
    assert render_model_ids_for_role(ROLE_REFERENCE, catalog, available) == ("a-visible",)
    assert render_model_ids_for_role(ROLE_MOVING, catalog, available) == ("p-visible",)


def test_single_visible_p_is_rendered_after_restore_without_selection() -> None:
    """복원 후 선택이 없어도 유일한 보이는 P만 표시·Frame All 대상으로 반환하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.viewport.renderer import render_model_ids_for_role

    first = SimpleNamespace(model_id="p-only", is_visible=True)
    second = SimpleNamespace(model_id="p-second", is_visible=True)
    catalog = SimpleNamespace(
        models={first.model_id: first, second.model_id: second},
        selected_moving_model_ids=(),
        inactive_models=lambda: (first,),
    )
    assert render_model_ids_for_role(ROLE_MOVING, catalog, {first.model_id}) == (
        first.model_id,
    )

    catalog.inactive_models = lambda: (first, second)
    assert render_model_ids_for_role(
        ROLE_MOVING,
        catalog,
        {first.model_id, second.model_id},
    ) == ()

    first.is_visible = False
    catalog.inactive_models = lambda: (first,)
    assert render_model_ids_for_role(ROLE_MOVING, catalog, {first.model_id}) == ()


def test_point_cloud_fixture_fallback_keeps_legacy_bounds_path() -> None:
    """Repository가 비어도 기존 point_clouds fixture의 좌표 경계를 반환하는지 확인합니다."""
    from tools.align.operators import workspace
    from tools.align.state import RUNTIME

    original_models = RUNTIME.model_repository.models
    original_point_clouds = RUNTIME.point_clouds
    try:
        RUNTIME.model_repository.models = {}
        RUNTIME.point_clouds = {
            "fixture": SimpleNamespace(
                positions=((-3.0, 2.0, 1.0), (5.0, -4.0, 6.0)),
            )
        }
        bounds = workspace._bounds_for_model_ids(["fixture"])
        assert bounds is not None
        minimum, maximum = bounds
        assert tuple(minimum) == (-3.0, -4.0, 1.0)
        assert tuple(maximum) == (5.0, 2.0, 6.0)
    finally:
        RUNTIME.model_repository.models = original_models
        RUNTIME.point_clouds = original_point_clouds


def test_real_colmap_obj_bounds_apply_display_and_alignment_transforms() -> None:
    """실제 COLMAP·OBJ 모델의 표시 축과 확정 Sim(3)을 적용한 혼합 경계를 검증합니다."""
    from tools.align.alignment import create_sim3_transform
    from tools.align.colmap_io.types import ColmapModelData, ImageTable, PointTable
    from tools.align.display_axes import create_model_display_transform, identity_display_transform
    from tools.align.mesh_io import TriangleMeshModelData
    from tools.align.operators import workspace
    from tools.align.state import RUNTIME

    colmap_positions = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 2.0, 3.0)),
        dtype=np.float64,
    )
    colmap_points = PointTable(
        point_ids=np.asarray((11, 12), dtype=np.uint64),
        positions=colmap_positions,
        colors=np.full((2, 3), 255, dtype=np.uint8),
        errors=np.zeros(2, dtype=np.float64),
        track_offsets=np.zeros(3, dtype=np.uint64),
        track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint32),
        display_indices=np.asarray((0, 1), dtype=np.uint32),
    )
    colmap_model = ColmapModelData(
        model_id="real-colmap",
        display_name="real-colmap",
        source_path=Path.cwd() / "real-colmap",
        source_format="text",
        cameras={},
        images=ImageTable(
            image_records={},
            point2d_offsets=np.asarray((0,), dtype=np.uint64),
            point2d_xy=np.empty((0, 2), dtype=np.float64),
            point2d_point3d_ids=np.empty(0, dtype=np.uint64),
        ),
        rigs={},
        frames={},
        points=colmap_points,
        bounds_min=colmap_positions.min(axis=0),
        bounds_max=colmap_positions.max(axis=0),
    )
    mesh_vertices = np.asarray(
        ((-1.0, 0.0, 0.0), (-1.0, 1.0, 0.0), (-2.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    mesh_model = TriangleMeshModelData(
        model_id="real-obj",
        display_name="real-obj",
        source_path=Path.cwd() / "real-obj.obj",
        vertices_float64=mesh_vertices,
        triangles_uint32=np.asarray(((0, 1, 2),), dtype=np.uint32),
        registration_vertex_indices=np.asarray((0, 1, 2), dtype=np.uint32),
        display_vertex_indices=np.asarray((0, 1, 2), dtype=np.uint32),
        bounds_min=mesh_vertices.min(axis=0),
        bounds_max=mesh_vertices.max(axis=0),
    )
    original_models = RUNTIME.model_repository.models
    original_display_transforms = RUNTIME.model_repository.display_transforms
    original_alignments = RUNTIME.model_repository.alignment_transforms
    colmap_display = create_model_display_transform("REALITY_CAPTURE", "Z", "-Y")
    mesh_display = identity_display_transform()
    colmap_alignment = create_sim3_transform(
        np.identity(3, dtype=np.float64),
        2.0,
        np.asarray((10.0, 0.0, 0.0), dtype=np.float64),
    )
    mesh_alignment = create_sim3_transform(
        np.asarray(((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)), dtype=np.float64),
        0.5,
        np.asarray((-2.0, 3.0, 0.0), dtype=np.float64),
    )
    try:
        RUNTIME.model_repository.models = {
            colmap_model.model_id: colmap_model,
            mesh_model.model_id: mesh_model,
        }
        RUNTIME.model_repository.display_transforms = {
            colmap_model.model_id: colmap_display,
            mesh_model.model_id: mesh_display,
        }
        RUNTIME.model_repository.alignment_transforms = {
            colmap_model.model_id: colmap_alignment,
            mesh_model.model_id: mesh_alignment,
        }
        colmap_bounds = workspace.effective_bounds_for_model_id(
            colmap_model.model_id,
            colmap_model.bounds_min,
            colmap_model.bounds_max,
        )
        mesh_bounds = workspace.effective_bounds_for_model_id(
            mesh_model.model_id,
            mesh_model.bounds_min,
            mesh_model.bounds_max,
        )
        mixed_bounds = workspace._bounds_for_model_ids(
            [colmap_model.model_id, mesh_model.model_id]
        )
        assert mixed_bounds is not None
        expected_min = np.minimum(colmap_bounds[0], mesh_bounds[0])
        expected_max = np.maximum(colmap_bounds[1], mesh_bounds[1])
        assert np.allclose(mixed_bounds[0], expected_min)
        assert np.allclose(mixed_bounds[1], expected_max)
        assert not np.allclose(mixed_bounds[0], np.minimum(colmap_model.bounds_min, mesh_model.bounds_min))
    finally:
        RUNTIME.model_repository.models = original_models
        RUNTIME.model_repository.display_transforms = original_display_transforms
        RUNTIME.model_repository.alignment_transforms = original_alignments


def test_mixed_saved_bounds_are_combined_and_invalid_model_cancels_atomically() -> None:
    """COLMAP·OBJ 저장 경계를 8모서리 API 결과로 결합하고 오류 모델은 전체 취소하는지 확인합니다."""
    from tools.align.operators import workspace
    from tools.align.state import RUNTIME

    original_models = RUNTIME.model_repository.models
    original_point_clouds = RUNTIME.point_clouds
    original_effective_bounds = workspace.effective_bounds_for_model_id
    calls: list[str] = []

    class NoPointIteration:
        """경계 외 원본 정점·면 접근이 발생하면 실패시키는 테스트 모델입니다."""

        model_id = ""
        bounds_min = (0.0, 0.0, 0.0)
        bounds_max = (1.0, 1.0, 1.0)

        @property
        def positions(self):
            """저장 경계만 소비하는지 확인하기 위해 원본 좌표 접근을 거부합니다."""
            raise AssertionError("화면 맞춤이 원본 좌표를 재순회했습니다.")

        @property
        def triangles(self):
            """저장 경계만 소비하는지 확인하기 위해 원본 면 접근을 거부합니다."""
            raise AssertionError("화면 맞춤이 원본 면을 재순회했습니다.")

    colmap_model = NoPointIteration()
    colmap_model.model_id = "colmap"
    obj_model = NoPointIteration()
    obj_model.model_id = "obj"
    try:
        RUNTIME.model_repository.models = {"colmap": colmap_model, "obj": obj_model}
        RUNTIME.point_clouds = {}

        def effective_bounds(model_id, _minimum, _maximum):
            """모델별 저장 경계 변환 결과를 기록해 결합 계약을 검증합니다."""
            calls.append(model_id)
            if model_id == "colmap":
                return (Vector((-4.0, -2.0, -1.0)), Vector((1.0, 3.0, 2.0)))
            return (Vector((8.0, 5.0, 4.0)), Vector((12.0, 9.0, 7.0)))

        workspace.effective_bounds_for_model_id = effective_bounds
        bounds = workspace._bounds_for_model_ids(["colmap", "obj"])
        assert bounds is not None
        assert calls == ["colmap", "obj"]
        assert tuple(bounds[0]) == (-4.0, -2.0, -1.0)
        assert tuple(bounds[1]) == (12.0, 9.0, 7.0)

        def invalid_effective_bounds(model_id, _minimum, _maximum):
            """한 모델의 잘못된 변환 결과가 전체 경계를 취소하도록 오류를 발생시킵니다."""
            if model_id == "obj":
                raise ValueError("invalid transform")
            return (Vector((-4.0, -2.0, -1.0)), Vector((1.0, 3.0, 2.0)))

        workspace.effective_bounds_for_model_id = invalid_effective_bounds
        try:
            workspace._bounds_for_model_ids(["colmap", "obj"])
        except workspace.WorkspaceConfigurationError:
            pass
        else:
            raise AssertionError("잘못된 모델을 제외한 부분 경계를 허용했습니다.")
    finally:
        workspace.effective_bounds_for_model_id = original_effective_bounds
        RUNTIME.model_repository.models = original_models
        RUNTIME.point_clouds = original_point_clouds


def test_layer_transform_error_cancels_fit_without_view_change() -> None:
    """손상된 레이어 변환 오류가 전체 맞춤을 취소하고 RegionView3D를 보존하는지 확인합니다."""
    from tools.align.alignment import LayerTransformError
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.operators import workspace
    from tools.align.state import RUNTIME

    original_models = RUNTIME.model_repository.models
    original_point_clouds = RUNTIME.point_clouds
    original_resolve_area = workspace.resolve_area
    original_render_ids = workspace.render_model_ids_for_role
    original_effective_bounds = workspace.effective_bounds_for_model_id
    region_3d = SimpleNamespace(
        view_location=Vector((7.0, 8.0, 9.0)),
        view_distance=3.0,
        view_rotation=Quaternion((1.0, 0.0, 0.0, 0.0)),
        view_perspective="PERSP",
    )
    area = SimpleNamespace(
        regions=[SimpleNamespace(type="WINDOW", width=640, height=480)],
        spaces=SimpleNamespace(active=SimpleNamespace(lens=50.0, region_3d=region_3d)),
        tag_redraw=lambda: None,
    )
    try:
        RUNTIME.model_repository.models = {
            "broken": SimpleNamespace(
                model_id="broken",
                bounds_min=(0.0, 0.0, 0.0),
                bounds_max=(1.0, 1.0, 1.0),
            )
        }
        RUNTIME.point_clouds = {}
        workspace.resolve_area = lambda _window, _role: area
        workspace.render_model_ids_for_role = lambda _role: ("broken",)

        def raise_layer_transform_error(*_args):
            """손상된 활성 레이어 변환을 시뮬레이션합니다."""
            raise LayerTransformError("테스트용 레이어 변환 손상")

        workspace.effective_bounds_for_model_id = raise_layer_transform_error
        before = (tuple(region_3d.view_location), region_3d.view_distance)
        try:
            workspace.fit_role_view(object(), ROLE_REFERENCE)
        except workspace.WorkspaceConfigurationError:
            pass
        else:
            raise AssertionError("레이어 변환 오류를 WorkspaceConfigurationError로 변환하지 않았습니다.")
        assert (tuple(region_3d.view_location), region_3d.view_distance) == before
    finally:
        workspace.effective_bounds_for_model_id = original_effective_bounds
        workspace.resolve_area = original_resolve_area
        workspace.render_model_ids_for_role = original_render_ids
        RUNTIME.model_repository.models = original_models
        RUNTIME.point_clouds = original_point_clouds


def test_bounds_reject_partial_repository_targets() -> None:
    """대상 중 일부 모델이 사라지면 부분 경계를 반환하지 않고 오류를 내는지 확인합니다."""
    from tools.align.operators import workspace
    from tools.align.state import RUNTIME

    original_models = RUNTIME.model_repository.models
    original_point_clouds = RUNTIME.point_clouds
    try:
        RUNTIME.model_repository.models = {
            "present": SimpleNamespace(
                model_id="present",
                bounds_min=(0.0, 0.0, 0.0),
                bounds_max=(1.0, 1.0, 1.0),
            )
        }
        RUNTIME.point_clouds = {}
        try:
            workspace._bounds_for_model_ids(["present", "missing"])
        except workspace.WorkspaceConfigurationError:
            pass
        else:
            raise AssertionError("누락 모델이 있는 부분 경계를 허용했습니다.")
    finally:
        RUNTIME.model_repository.models = original_models
        RUNTIME.point_clouds = original_point_clouds


def test_fit_area_preserves_rotation_and_perspective() -> None:
    """화면 맞춤이 RegionView3D의 회전과 투영 모드를 변경하지 않는지 확인합니다."""
    from tools.align.operators.workspace import _fit_area_to_bounds

    rotation = Quaternion((0.9238795, 0.0, 0.3826834, 0.0))
    region_3d = SimpleNamespace(
        view_location=Vector((9.0, 8.0, 7.0)),
        view_distance=2.0,
        view_rotation=rotation.copy(),
        view_perspective="ORTHO",
    )
    area = SimpleNamespace(
        regions=[SimpleNamespace(type="WINDOW", width=800, height=600)],
        spaces=SimpleNamespace(
            active=SimpleNamespace(lens=50.0, clip_end=1.0, region_3d=region_3d)
        ),
        tag_redraw=lambda: None,
    )
    _fit_area_to_bounds(area, (Vector((-2.0, -1.0, -3.0)), Vector((4.0, 3.0, 5.0))))
    assert tuple(region_3d.view_rotation) == tuple(rotation)
    assert region_3d.view_perspective == "ORTHO"
    assert tuple(region_3d.view_location) == (1.0, 1.0, 1.0)
    assert region_3d.view_distance > 0.0
    assert area.spaces.active.clip_end > region_3d.view_distance


def test_fit_area_rejects_invalid_bounds_before_writing_view() -> None:
    """유효하지 않은 경계는 RegionView3D를 일부라도 바꾸지 않고 취소하는지 확인합니다."""
    from tools.align.operators import workspace

    region_3d = SimpleNamespace(
        view_location=Vector((9.0, 8.0, 7.0)),
        view_distance=2.0,
        view_rotation=Quaternion((1.0, 0.0, 0.0, 0.0)),
        view_perspective="PERSP",
    )
    area = SimpleNamespace(
        regions=[SimpleNamespace(type="WINDOW", width=800, height=600)],
        spaces=SimpleNamespace(
            active=SimpleNamespace(lens=50.0, clip_end=1000.0, region_3d=region_3d)
        ),
        tag_redraw=lambda: None,
    )
    before = (
        tuple(region_3d.view_location),
        region_3d.view_distance,
        area.spaces.active.clip_end,
    )
    try:
        workspace._fit_area_to_bounds(area, (Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 1.0))))
    except workspace.WorkspaceConfigurationError:
        pass
    else:
        raise AssertionError("역전된 경계를 거부하지 않았습니다.")
    assert (
        tuple(region_3d.view_location),
        region_3d.view_distance,
        area.spaces.active.clip_end,
    ) == before


def test_frame_visible_role_under_mouse_consumes_only_a_p_viewport() -> None:
    """마우스 아래 A/P가 발견될 때만 맞춤을 소비하고 외부 좌표는 통과시키는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.operators import view_framing

    original_find = view_framing.find_viewport_under_mouse
    original_fit = view_framing.fit_role_view
    original_status = view_framing._set_frame_status
    original_session = view_framing.is_session_active_for_window
    calls: list[tuple[object, str]] = []
    try:
        view_framing.find_viewport_under_mouse = lambda _window, _x, _y: (
            object(),
            object(),
            ROLE_MOVING,
        )
        view_framing.is_session_active_for_window = lambda _window: True
        view_framing.fit_role_view = lambda window, role: calls.append((window, role)) or True
        view_framing._set_frame_status = lambda *_args: None
        window = object()
        context = SimpleNamespace(window=window)
        event = SimpleNamespace(mouse_x=40, mouse_y=50)
        assert view_framing.frame_visible_role_under_mouse(context, event) is True
        assert calls == [(window, ROLE_MOVING)]

        view_framing.find_viewport_under_mouse = lambda _window, _x, _y: None
        assert view_framing.frame_visible_role_under_mouse(context, event) is False
    finally:
        view_framing.find_viewport_under_mouse = original_find
        view_framing.fit_role_view = original_fit
        view_framing._set_frame_status = original_status
        view_framing.is_session_active_for_window = original_session


def test_frame_helper_rejects_inactive_session_before_viewport_lookup() -> None:
    """세션이 끝난 일반 작업공간에서는 Frame All 이벤트를 소비하지 않는지 확인합니다."""
    from tools.align.operators import view_framing

    original_session = view_framing.is_session_active_for_window
    original_find = view_framing.find_viewport_under_mouse
    try:
        view_framing.is_session_active_for_window = lambda _window: False
        view_framing.find_viewport_under_mouse = lambda *_args: (_ for _ in ()).throw(
            AssertionError("비활성 세션에서 뷰포트를 조회했습니다.")
        )
        assert view_framing.frame_visible_role_under_mouse(
            SimpleNamespace(window=object()),
            SimpleNamespace(mouse_x=1, mouse_y=1),
        ) is False
    finally:
        view_framing.is_session_active_for_window = original_session
        view_framing.find_viewport_under_mouse = original_find


def test_frame_all_gizmo_setup_sets_visible_home_button_and_binds_role() -> None:
    """기즈모 setup이 가시성 속성·Home 아이콘·현재 역할을 직접 연결하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.operators import view_framing

    class FakeButton:
        """Blender Gizmo button 속성을 기록하는 테스트 대역입니다."""

        def __init__(self):
            """검사 대상 속성과 operator 연결 상태를 초기화합니다."""
            self.operator = SimpleNamespace(role=None)

        def target_set_operator(self, _operator_id):
            """기존 fit_view operator 연결 결과를 반환합니다."""
            return self.operator

    class FakeGizmos:
        """GizmoGroup.gizmos.new 호출을 기록하는 테스트 대역입니다."""

        def __init__(self):
            """생성된 단일 기즈모를 보관합니다."""
            self.button = FakeButton()

        def new(self, gizmo_type):
            """GIZMO_GT_button_2d 하나만 생성하도록 검증합니다."""
            assert gizmo_type == "GIZMO_GT_button_2d"
            return self.button

    original_role = view_framing._role_for_context
    try:
        view_framing._role_for_context = lambda _context: ROLE_MOVING
        gizmos = FakeGizmos()
        group = SimpleNamespace(gizmos=gizmos)
        view_framing.COLMAPALIGN_GGT_frame_all.setup(group, object())
        button = gizmos.button
        assert button.icon == "HOME"
        assert button.draw_options == {"BACKDROP", "OUTLINE"}
        assert button.alpha > 0.0
        assert button.alpha_highlight > 0.0
        assert button.color_highlight
        assert button.scale_basis == 14.0
        assert button.operator.role == ROLE_MOVING
    finally:
        view_framing._role_for_context = original_role


def test_frame_all_view_menu_only_draws_for_active_a_or_p_session() -> None:
    """View 메뉴가 활성 A/P에만 역할 operator를 추가하고 일반 컨텍스트에는 비어 있는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.operators import view_framing

    class FakeLayout:
        """메뉴 operator 호출과 separator를 기록하는 테스트 레이아웃입니다."""

        def __init__(self):
            """호출 기록을 초기화합니다."""
            self.calls = []

        def operator(self, operator_id, **kwargs):
            """메뉴 operator 속성과 표시 인자를 기록합니다."""
            operator = SimpleNamespace(role=None)
            self.calls.append((operator_id, kwargs, operator))
            return operator

        def separator(self):
            """메뉴 separator 호출을 기록합니다."""
            self.calls.append(("SEPARATOR", {}, None))

    class FakeMenu:
        """Blender 메뉴의 layout 필드를 제공하는 대역입니다."""

        def __init__(self):
            """가짜 레이아웃을 연결합니다."""
            self.layout = FakeLayout()

    original_session = view_framing.is_session_active_for_window
    original_role = view_framing.role_for_area
    try:
        view_framing.is_session_active_for_window = lambda _window: True
        view_framing.role_for_area = lambda _window, _area: ROLE_MOVING
        active_menu = FakeMenu()
        view_framing.draw_frame_all_view_menu(
            active_menu,
            SimpleNamespace(window=object(), area=object()),
        )
        assert active_menu.layout.calls[0][0] == "colmap_align.fit_view"
        assert active_menu.layout.calls[0][2].role == ROLE_MOVING

        view_framing.is_session_active_for_window = lambda _window: False
        inactive_menu = FakeMenu()
        view_framing.draw_frame_all_view_menu(
            inactive_menu,
            SimpleNamespace(window=object(), area=object()),
        )
        assert inactive_menu.layout.calls == []
    finally:
        view_framing.is_session_active_for_window = original_session
        view_framing.role_for_area = original_role


def test_frame_all_gizmo_contract_and_draw_prepare() -> None:
    """기즈모가 내장 Home 아이콘과 오른쪽 아래 위치·숨김 규칙을 사용하는지 확인합니다."""
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.operators import view_framing

    gizmo_class = view_framing.COLMAPALIGN_GGT_frame_all
    assert gizmo_class.bl_space_type == "VIEW_3D"
    assert gizmo_class.bl_region_type == "WINDOW"
    assert gizmo_class.bl_options == {"PERSISTENT", "SCALE"}

    original_role = view_framing._role_for_context
    original_ids = view_framing.render_model_ids_for_role
    try:
        view_framing._role_for_context = lambda _context: ROLE_REFERENCE
        view_framing.render_model_ids_for_role = lambda _role: ("a-1",)
        gizmo = SimpleNamespace(
            matrix_basis=Matrix.Identity(4),
            hide=False,
            scale_basis=0.0,
        )
        group = SimpleNamespace()
        group._frame_all_button = gizmo
        gizmo_class.draw_prepare(group, SimpleNamespace(region=SimpleNamespace(width=200, height=100)))
        assert gizmo.hide is False
        assert gizmo.scale_basis == 14.0
        assert gizmo.matrix_basis[0][3] == 172.0
        assert gizmo.matrix_basis[1][3] == 28.0

        view_framing.render_model_ids_for_role = lambda _role: ()
        gizmo_class.draw_prepare(group, SimpleNamespace(region=SimpleNamespace(width=20, height=20)))
        assert gizmo.hide is True
        assert gizmo.matrix_basis[0][3] == 28.0
        assert gizmo.matrix_basis[1][3] == 28.0
        assert gizmo.matrix_basis[1][3] == 28.0
    finally:
        view_framing._role_for_context = original_role
        view_framing.render_model_ids_for_role = original_ids


def test_successful_import_fits_visible_role_group_for_colmap_and_obj() -> None:
    """COLMAP·OBJ 성공 가져오기가 단일 모델이 아닌 해당 역할 표시 집합을 맞추는지 확인합니다."""
    from tools.align.constants import (
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.operators import import_model, workspace
    from tools.align.state import RUNTIME

    original_models = RUNTIME.model_catalog.models
    original_find_main_window = workspace.find_main_window
    original_fit_role_view = workspace.fit_role_view
    main_window = object()
    fit_calls = []
    try:
        RUNTIME.model_catalog.models = {
            "colmap-import": SimpleNamespace(
                model_id="colmap-import",
                registration_status=MODEL_STATUS_REGISTERED,
            ),
            "obj-import": SimpleNamespace(
                model_id="obj-import",
                registration_status=MODEL_STATUS_PENDING,
            ),
        }
        workspace.find_main_window = lambda _manager, _window: main_window

        def record_fit(window, role, **kwargs):
            """자동 맞춤이 명시 모델 ID 없이 역할 전체 경로를 호출했는지 기록합니다."""
            fit_calls.append((window, role, kwargs))
            return True

        workspace.fit_role_view = record_fit
        context = SimpleNamespace(window_manager=object(), window=object())
        import_model.fit_after_successful_import(context, "colmap-import")
        import_model.fit_after_successful_import(context, "obj-import")
        assert fit_calls == [
            (main_window, ROLE_REFERENCE, {}),
            (main_window, ROLE_MOVING, {}),
        ]
    finally:
        workspace.fit_role_view = original_fit_role_view
        workspace.find_main_window = original_find_main_window
        RUNTIME.model_catalog.models = original_models


def run_all() -> None:
    """Frame All 표시 집합·경계·입력·기즈모 단위 검사를 실행합니다."""
    _add_source_path()
    test_fit_role_view_uses_render_model_ids_for_default_targets()
    test_render_model_ids_match_visible_a_and_selected_visible_p_subset()
    test_single_visible_p_is_rendered_after_restore_without_selection()
    test_point_cloud_fixture_fallback_keeps_legacy_bounds_path()
    test_real_colmap_obj_bounds_apply_display_and_alignment_transforms()
    test_mixed_saved_bounds_are_combined_and_invalid_model_cancels_atomically()
    test_layer_transform_error_cancels_fit_without_view_change()
    test_bounds_reject_partial_repository_targets()
    test_fit_area_preserves_rotation_and_perspective()
    test_fit_area_rejects_invalid_bounds_before_writing_view()
    test_frame_visible_role_under_mouse_consumes_only_a_p_viewport()
    test_frame_helper_rejects_inactive_session_before_viewport_lookup()
    test_frame_all_gizmo_setup_sets_visible_home_button_and_binds_role()
    test_frame_all_view_menu_only_draws_for_active_a_or_p_session()
    test_frame_all_gizmo_contract_and_draw_prepare()
    test_successful_import_fits_visible_role_group_for_colmap_and_obj()
    print("test_view_framing: 통과")


if __name__ == "__main__":
    run_all()
