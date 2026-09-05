"""마커 미리보기의 P/A 일반 렌더 합성과 GPU 제출 계약을 검증합니다."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Iterator

import bpy
import numpy as np
from mathutils import Matrix


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _point_table(
    point_ids: tuple[int, ...],
    positions: tuple[tuple[float, float, float], ...],
):
    """점군 합성·GPU 배치 검증에 필요한 최소 PointTable을 생성합니다."""
    from tools.align.colmap_io.types import PointTable

    count = len(point_ids)
    return PointTable(
        point_ids=np.asarray(point_ids, dtype=np.uint64),
        positions=np.asarray(positions, dtype=np.float64),
        colors=np.asarray(
            tuple(
                ((index * 71) % 256, (80 + index * 53) % 256, (170 + index * 29) % 256)
                for index in range(count)
            ),
            dtype=np.uint8,
        ),
        errors=np.zeros(count, dtype=np.float64),
        track_offsets=np.zeros(count + 1, dtype=np.uint64),
        track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint64),
        display_indices=np.arange(count, dtype=np.uint32),
    )


def _model(
    model_id: str,
    positions: tuple[tuple[float, float, float], ...],
    *,
    revision: int,
):
    """렌더러가 실제 PointTable로 인식하는 최소 모델 데이터를 생성합니다."""
    point_ids = tuple(range(10_001, 10_001 + len(positions)))
    return SimpleNamespace(
        model_id=model_id,
        geometry_revision=revision,
        points=_point_table(point_ids, positions),
    )


@contextmanager
def _preview_draw_fixture() -> Iterator[SimpleNamespace]:
    """전역 Runtime에 A·P 모델과 비항등 marker preview를 잠시 구성합니다."""
    from tools.align.alignment import (
        compose_sim3_transforms,
        create_sim3_transform,
        identity_sim3_transform,
    )
    from tools.align.constants import (
        MARKER_ALIGNMENT_PHASE_IDLE,
        MARKER_ALIGNMENT_PHASE_PREVIEW,
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
    )
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import ModelCatalogRecord, ModelCatalogState, RUNTIME

    original_catalog = RUNTIME.model_catalog
    original_models = dict(RUNTIME.model_repository.models)
    original_source_index = dict(RUNTIME.model_repository.source_path_index)
    original_display_transforms = dict(RUNTIME.model_repository.display_transforms)
    original_alignments = dict(RUNTIME.model_repository.alignment_transforms)
    original_batches = dict(RUNTIME.gpu_batches)
    preview_state = RUNTIME.marker_alignment
    original_phase = preview_state.phase
    original_result = preview_state.result
    original_error = preview_state.last_error
    original_started_at = preview_state.started_at
    original_view_snapshot = preview_state.p_view_snapshot

    reference_model = _model(
        "preview-reference",
        ((-0.45, -0.15, 0.0), (-0.05, 0.35, 0.0), (0.35, -0.30, 0.0)),
        revision=3,
    )
    moving_model = _model(
        "preview-moving",
        ((-0.20, -0.10, 0.0), (0.05, 0.20, 0.0), (0.25, -0.18, 0.0)),
        revision=7,
    )
    identity = identity_display_transform()
    committed_alignment = create_sim3_transform(
        np.identity(3, dtype=np.float64),
        1.5,
        np.asarray((0.10, -0.08, 0.0), dtype=np.float64),
    )
    delta_transform = create_sim3_transform(
        np.identity(3, dtype=np.float64),
        0.75,
        np.asarray((0.20, 0.12, 0.0), dtype=np.float64),
    )
    preview_alignment = compose_sim3_transforms(delta_transform, committed_alignment)

    RUNTIME.model_repository.models.clear()
    RUNTIME.model_repository.models.update(
        {
            reference_model.model_id: reference_model,
            moving_model.model_id: moving_model,
        }
    )
    RUNTIME.model_repository.source_path_index.clear()
    RUNTIME.model_repository.display_transforms.clear()
    RUNTIME.model_repository.display_transforms.update(
        {
            reference_model.model_id: identity,
            moving_model.model_id: identity,
        }
    )
    RUNTIME.model_repository.alignment_transforms.clear()
    RUNTIME.model_repository.alignment_transforms.update(
        {
            reference_model.model_id: identity_sim3_transform(),
            moving_model.model_id: committed_alignment,
        }
    )
    RUNTIME.model_catalog = ModelCatalogState(
        models={
            reference_model.model_id: ModelCatalogRecord(
                model_id=reference_model.model_id,
                display_name="A",
                load_order=1,
                registration_status=MODEL_STATUS_REGISTERED,
                is_anchor=True,
            ),
            moving_model.model_id: ModelCatalogRecord(
                model_id=moving_model.model_id,
                display_name="P",
                load_order=2,
                registration_status=MODEL_STATUS_PENDING,
            ),
        },
        model_order=[reference_model.model_id, moving_model.model_id],
        selected_model_id=moving_model.model_id,
        selected_model_ids=(moving_model.model_id,),
    )
    preview_state.phase = MARKER_ALIGNMENT_PHASE_IDLE
    preview_state.result = None
    preview_state.last_error = ""
    preview_state.started_at = None
    preview_state.p_view_snapshot = None
    RUNTIME.gpu_batches.clear()

    try:
        yield SimpleNamespace(
            reference_model=reference_model,
            moving_model=moving_model,
            committed_alignment=committed_alignment,
            delta_transform=delta_transform,
            preview_alignment=preview_alignment,
            activate_preview=lambda: _activate_preview(
                MARKER_ALIGNMENT_PHASE_PREVIEW,
                moving_model.model_id,
                preview_alignment,
            ),
        )
    finally:
        RUNTIME.model_catalog = original_catalog
        RUNTIME.model_repository.models.clear()
        RUNTIME.model_repository.models.update(original_models)
        RUNTIME.model_repository.source_path_index.clear()
        RUNTIME.model_repository.source_path_index.update(original_source_index)
        RUNTIME.model_repository.display_transforms.clear()
        RUNTIME.model_repository.display_transforms.update(original_display_transforms)
        RUNTIME.model_repository.alignment_transforms.clear()
        RUNTIME.model_repository.alignment_transforms.update(original_alignments)
        RUNTIME.gpu_batches.clear()
        RUNTIME.gpu_batches.update(original_batches)
        preview_state.phase = original_phase
        preview_state.result = original_result
        preview_state.last_error = original_error
        preview_state.started_at = original_started_at
        preview_state.p_view_snapshot = original_view_snapshot


def _activate_preview(phase: str, moving_model_id: str, preview_alignment: object) -> None:
    """테스트 계약에 필요한 최소 marker preview 결과를 전역 Runtime에 등록합니다."""
    from tools.align.state import RUNTIME

    RUNTIME.marker_alignment.phase = phase
    RUNTIME.marker_alignment.result = SimpleNamespace(
        moving_model_id=moving_model_id,
        preview_alignment=preview_alignment,
    )


def _pass_signature(draw_passes: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
    """점군 제출 객체를 순서·역할·합성 상태 비교용 튜플로 정규화합니다."""
    return tuple(
        (
            draw_pass.model_ids,
            draw_pass.role,
            float(draw_pass.opacity),
            bool(draw_pass.depth_write),
            draw_pass.depth_test,
        )
        for draw_pass in draw_passes
    )


def test_preview_draw_passes_keep_a_view_and_overlay_reference_normally_in_p_view() -> None:
    """역할별 표시를 유지하고 preview P 뷰의 A도 일반 불투명 상태로 추가하는지 검증합니다."""
    from tools.align.alignment import cancel_marker_preview
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import RUNTIME
    from tools.align.viewport.renderer import point_cloud_draw_passes_for_role

    with _preview_draw_fixture() as fixture:
        normal_reference = _pass_signature(point_cloud_draw_passes_for_role(ROLE_REFERENCE))
        normal_moving = _pass_signature(point_cloud_draw_passes_for_role(ROLE_MOVING))
        assert normal_reference == (
            ((fixture.reference_model.model_id,), ROLE_REFERENCE, 1.0, True, None),
        )
        assert normal_moving == (
            ((fixture.moving_model.model_id,), ROLE_MOVING, 1.0, True, None),
        )

        fixture.activate_preview()
        preview_reference = _pass_signature(point_cloud_draw_passes_for_role(ROLE_REFERENCE))
        preview_moving = _pass_signature(point_cloud_draw_passes_for_role(ROLE_MOVING))
        assert preview_reference == normal_reference
        assert preview_moving == (
            ((fixture.moving_model.model_id,), ROLE_MOVING, 1.0, True, None),
            (
                (fixture.reference_model.model_id,),
                ROLE_REFERENCE,
                1.0,
                True,
                None,
            ),
        )

        assert cancel_marker_preview(RUNTIME) is None
        assert _pass_signature(point_cloud_draw_passes_for_role(ROLE_REFERENCE)) == normal_reference
        assert _pass_signature(point_cloud_draw_passes_for_role(ROLE_MOVING)) == normal_moving


def test_preview_moving_mvp_uses_delta_times_committed_alignment_exactly_once() -> None:
    """P GPU MVP가 preview alignment를 한 번만 적용하고 A는 확정 행렬을 유지하는지 검증합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.viewport import renderer

    with _preview_draw_fixture() as fixture:
        fixture.activate_preview()
        base_matrix = Matrix.Identity(4)
        moving_mvp = np.asarray(
            renderer._model_view_projection_matrix(base_matrix, fixture.moving_model.model_id),
            dtype=np.float64,
        )
        reference_mvp = np.asarray(
            renderer._model_view_projection_matrix(base_matrix, fixture.reference_model.model_id),
            dtype=np.float64,
        )
        expected_moving = fixture.preview_alignment.matrix_float64
        expected_reference = np.identity(4, dtype=np.float64)
        doubled_delta = fixture.delta_transform.matrix_float64 @ expected_moving

        np.testing.assert_allclose(moving_mvp, expected_moving, rtol=0.0, atol=1.0e-7)
        np.testing.assert_allclose(reference_mvp, expected_reference, rtol=0.0, atol=1.0e-7)
        assert not np.allclose(moving_mvp, doubled_delta, rtol=0.0, atol=1.0e-7)

        draw_passes = renderer.point_cloud_draw_passes_for_role(ROLE_MOVING)
        assert draw_passes[0].role == ROLE_MOVING
        assert draw_passes[1].role == ROLE_REFERENCE
        assert draw_passes[1].opacity == 1.0
        assert draw_passes[1].depth_write is True
        assert draw_passes[1].depth_test is None


def test_point_cloud_submitter_executes_each_preview_pass_once() -> None:
    """P preview 점군 제출기가 불투명 P와 A pass를 순서대로 한 번씩 실행하는지 검증합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.viewport import renderer

    submitted: list[tuple[tuple[str, ...], str, float, bool, str | None, bool | None]] = []
    original_submitter = renderer._draw_model_point_clouds
    try:
        with _preview_draw_fixture() as fixture:
            fixture.activate_preview()
            renderer._draw_model_point_clouds = (
                lambda model_ids, role, *, opacity, depth_write, depth_test, use_colmap_rgb: submitted.append(
                    (model_ids, role, opacity, depth_write, depth_test, use_colmap_rgb)
                )
            )
            renderer._draw_point_clouds(ROLE_MOVING)
    finally:
        renderer._draw_model_point_clouds = original_submitter

    assert submitted == [
        (("preview-moving",), ROLE_MOVING, 1.0, True, None, None),
        (("preview-reference",), ROLE_REFERENCE, 1.0, True, None, None),
    ]


def test_preview_geometry_does_not_submit_duplicate_point_clouds() -> None:
    """대표 삼각형 처리기가 점군 pass를 중복 제출하지 않고 도형만 담당하는지 검증합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.viewport import renderer

    submitted: list[object] = []
    original_submitter = renderer._draw_model_point_clouds
    original_triangle_drawer = renderer._draw_preview_triangle
    original_triangle_vertices = renderer.preview_triangle_vertices
    try:
        with _preview_draw_fixture() as fixture:
            fixture.activate_preview()
            renderer._draw_model_point_clouds = lambda *args, **kwargs: submitted.append((args, kwargs))
            renderer.preview_triangle_vertices = lambda _result: (
                np.zeros((3, 3), dtype=np.float32),
                np.zeros((3, 3), dtype=np.float32),
            )
            renderer._draw_preview_triangle = lambda *args, **kwargs: None
            renderer._draw_marker_preview_geometry(ROLE_MOVING)
    finally:
        renderer._draw_model_point_clouds = original_submitter
        renderer._draw_preview_triangle = original_triangle_drawer
        renderer.preview_triangle_vertices = original_triangle_vertices

    assert submitted == []


def test_preview_triangle_is_hidden_after_icp_alignment_finishes() -> None:
    """ICP 결과 미리보기에서는 정렬용 대표 삼각형만 더 이상 그리지 않는지 검증합니다."""
    from tools.align.constants import ICP_ALIGNMENT_PHASE_PREVIEW, ROLE_MOVING
    from tools.align.state import RUNTIME
    from tools.align.viewport import renderer

    submitted: list[np.ndarray] = []
    original_icp_phase = RUNTIME.icp_alignment.phase
    original_triangle_drawer = renderer._draw_preview_triangle
    original_triangle_vertices = renderer.preview_triangle_vertices
    try:
        with _preview_draw_fixture() as fixture:
            fixture.activate_preview()
            renderer.preview_triangle_vertices = lambda _result: (
                np.zeros((3, 3), dtype=np.float32),
                np.ones((3, 3), dtype=np.float32),
            )
            renderer._draw_preview_triangle = (
                lambda vertices, _face_color, _edge_color: submitted.append(vertices)
            )

            renderer._draw_marker_preview_geometry(ROLE_MOVING)
            assert len(submitted) == 2

            submitted.clear()
            RUNTIME.icp_alignment.phase = ICP_ALIGNMENT_PHASE_PREVIEW
            renderer._draw_marker_preview_geometry(ROLE_MOVING)
            assert submitted == []
    finally:
        RUNTIME.icp_alignment.phase = original_icp_phase
        renderer._draw_preview_triangle = original_triangle_drawer
        renderer.preview_triangle_vertices = original_triangle_vertices


def test_preview_triangle_faces_remain_translucent() -> None:
    """일반 형상과 달리 세 마커를 잇는 A/P 삼각형 면만 반투명인지 검증합니다."""
    from tools.align.viewport import renderer

    face_colors = (
        renderer._PREVIEW_REFERENCE_TRIANGLE_FACE_COLOR,
        renderer._PREVIEW_MOVING_TRIANGLE_FACE_COLOR,
    )
    assert all(0.0 < color[3] < 1.0 for color in face_colors)


def test_normal_draw_restores_depth_state_and_uses_full_opacity() -> None:
    """일반 점군 제출이 깊이 쓰기를 복원하고 완전 불투명으로 그리는지 검증합니다."""
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.viewport import renderer

    class FakeState:
        """GPU depth 상태 호출을 기록하는 최소 대체 객체입니다."""

        def __init__(self) -> None:
            """검증 시작 전 GPU 깊이 상태를 비기본값으로 구성합니다."""
            self.depth_test = "LESS_EQUAL"
            self.depth_mask = False
            self.events: list[tuple[str, object]] = []

        def depth_test_get(self) -> str:
            """현재 depth test 상태를 반환합니다."""
            return self.depth_test

        def depth_test_set(self, value: str) -> None:
            """depth test 변경을 기록합니다."""
            self.events.append(("depth_test", value))
            self.depth_test = value

        def depth_mask_get(self) -> bool:
            """현재 depth write 상태를 반환합니다."""
            return self.depth_mask

        def depth_mask_set(self, value: bool) -> None:
            """depth write 변경을 기록합니다."""
            self.events.append(("depth_mask", bool(value)))
            self.depth_mask = bool(value)

        def point_size_set(self, value: float) -> None:
            """점 크기 변경을 기록합니다."""
            self.events.append(("point_size", float(value)))

    class FakeMatrix:
        """점군 MVP 계산에 필요한 단위 행렬만 반환합니다."""

        @staticmethod
        def get_projection_matrix() -> Matrix:
            """단위 투영 행렬을 반환합니다."""
            return Matrix.Identity(4)

        @staticmethod
        def get_model_view_matrix() -> Matrix:
            """단위 모델뷰 행렬을 반환합니다."""
            return Matrix.Identity(4)

    class FakeShader:
        """uniform 기록과 bind 여부를 보관하는 최소 셰이더입니다."""

        def __init__(self) -> None:
            """빈 uniform 기록을 초기화합니다."""
            self.uniforms: list[tuple[str, object]] = []
            self.bind_count = 0

        def bind(self) -> None:
            """셰이더 bind 호출을 기록합니다."""
            self.bind_count += 1

        def uniform_float(self, name: str, value: object) -> None:
            """셰이더 uniform 값을 기록합니다."""
            self.uniforms.append((name, value))

    class FakeBatch:
        """GPU batch draw 호출 횟수를 기록하는 최소 대체 객체입니다."""

        def __init__(self) -> None:
            """draw 호출 횟수를 0으로 초기화합니다."""
            self.draw_count = 0

        def draw(self, shader: object) -> None:
            """실제 경로의 batch.draw 호출을 기록합니다."""
            assert shader is fake_shader
            self.draw_count += 1

    fake_state = FakeState()
    fake_shader = FakeShader()
    fake_batch = FakeBatch()
    fake_gpu = SimpleNamespace(state=fake_state, matrix=FakeMatrix())
    fake_bpy = SimpleNamespace(
        context=SimpleNamespace(
            scene=SimpleNamespace(
                colmap_align_settings=SimpleNamespace(
                    use_colmap_rgb=True,
                    point_size=3.0,
                )
            )
        )
    )
    original_gpu = renderer.gpu
    original_bpy = renderer.bpy
    original_shader_factory = renderer._point_shader
    original_batch_resolver = renderer._prepared_batch_for_model
    original_mvp_builder = renderer._model_view_projection_matrix
    try:
        renderer.gpu = fake_gpu
        renderer.bpy = fake_bpy
        renderer._point_shader = lambda: fake_shader
        renderer._prepared_batch_for_model = lambda model_id: fake_batch if model_id == "normal-a" else None
        renderer._model_view_projection_matrix = lambda _base, _model_id: Matrix.Identity(4)
        renderer._draw_model_point_clouds(
            ("normal-a",),
            ROLE_REFERENCE,
        )

        assert fake_batch.draw_count == 1
        assert ("depth_test", "NONE") not in fake_state.events
        assert ("depth_test", "LESS_EQUAL") in fake_state.events
        assert ("depth_mask", True) in fake_state.events
        assert fake_state.depth_test == "LESS_EQUAL"
        assert fake_state.depth_mask is False
        assert ("opacity", 1.0) in fake_shader.uniforms
        assert fake_shader.bind_count == 1
    finally:
        renderer.gpu = original_gpu
        renderer.bpy = original_bpy
        renderer._point_shader = original_shader_factory
        renderer._prepared_batch_for_model = original_batch_resolver
        renderer._model_view_projection_matrix = original_mvp_builder


def test_preview_p_view_submits_reused_real_gpu_batches() -> None:
    """preview P 뷰의 불투명 P와 A가 실제 Blender GPU batch로 제출되는지 검증합니다."""
    import gpu

    from tools.align import registration
    from tools.align.constants import ROLE_MOVING
    from tools.align.viewport import renderer

    registration.register()
    original_shader = renderer._POINT_SHADER
    try:
        with _preview_draw_fixture() as fixture:
            fixture.activate_preview()
            settings = bpy.context.scene.colmap_align_settings
            settings.use_colmap_rgb = True
            settings.point_size = 5.0
            renderer._POINT_SHADER = None
            gpu.init()
            moving_batch = renderer.prepare_gpu_batch_for_model(
                fixture.moving_model.model_id,
                fixture.moving_model,
            )
            reference_batch = renderer.prepare_gpu_batch_for_model(
                fixture.reference_model.model_id,
                fixture.reference_model,
            )
            assert moving_batch is not reference_batch

            offscreen = gpu.types.GPUOffScreen(64, 64)
            try:
                with offscreen.bind():
                    framebuffer = gpu.state.active_framebuffer_get()
                    framebuffer.clear(color=(0.0, 0.0, 0.0, 0.0), depth=1.0)
                    gpu.matrix.push()
                    try:
                        gpu.matrix.load_matrix(Matrix.Identity(4))
                        gpu.matrix.load_projection_matrix(Matrix.Identity(4))
                        renderer._draw_point_clouds(ROLE_MOVING)
                    finally:
                        gpu.matrix.pop()
            finally:
                offscreen.free()
    finally:
        renderer._POINT_SHADER = original_shader
        registration.unregister()


def run_all() -> None:
    """마커 preview 일반 렌더 합성과 실제 GPU 제출 회귀를 순서대로 실행합니다."""
    _add_source_path()
    test_preview_draw_passes_keep_a_view_and_overlay_reference_normally_in_p_view()
    test_preview_moving_mvp_uses_delta_times_committed_alignment_exactly_once()
    test_point_cloud_submitter_executes_each_preview_pass_once()
    test_preview_geometry_does_not_submit_duplicate_point_clouds()
    test_preview_triangle_is_hidden_after_icp_alignment_finishes()
    test_preview_triangle_faces_remain_translucent()
    test_normal_draw_restores_depth_state_and_uses_full_opacity()
    test_preview_p_view_submits_reused_real_gpu_batches()
    print("test_marker_preview_combined_clouds: 통과")


if __name__ == "__main__":
    run_all()
