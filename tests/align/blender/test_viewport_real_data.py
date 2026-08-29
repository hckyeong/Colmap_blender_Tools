"""실제 PointTable 기반 LOD·GPU 배치·벡터화 피킹 경계를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import bpy
import numpy as np
from mathutils import Matrix


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _point_table(
    point_ids: list[int],
    positions: list[tuple[float, float, float]],
    colors: list[tuple[int, int, int]],
    display_indices: list[int],
):
    """테스트에 필요한 최소 관측 트랙의 실제 PointTable을 생성합니다."""
    from tools.align.colmap_io.types import PointTable

    point_count = len(point_ids)
    return PointTable(
        point_ids=np.asarray(point_ids, dtype=np.uint64),
        positions=np.asarray(positions, dtype=np.float64),
        colors=np.asarray(colors, dtype=np.uint8),
        errors=np.zeros(point_count, dtype=np.float64),
        track_offsets=np.zeros(point_count + 1, dtype=np.uint64),
        track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint64),
        display_indices=np.asarray(display_indices, dtype=np.uint32),
    )


def _active_catalog(model_ids: tuple[str, ...]):
    """지정 모델 전체를 A 그룹으로 표시하는 최소 카탈로그를 생성합니다."""
    from tools.align.constants import MODEL_STATUS_REGISTERED
    from tools.align.state import ModelCatalogRecord, ModelCatalogState

    records = {
        model_id: ModelCatalogRecord(
            model_id=model_id,
            display_name=model_id,
            load_order=index + 1,
            registration_status=MODEL_STATUS_REGISTERED,
            is_anchor=index == 0,
        )
        for index, model_id in enumerate(model_ids)
    }
    return ModelCatalogState(models=records, model_order=list(model_ids))


def _replace_repository_models(
    models: dict[str, object],
    transforms: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, str], dict[str, object], dict[str, object]]:
    """테스트 동안 Runtime 저장소 모델·표시 축·확정 alignment를 함께 교체합니다."""
    from tools.align.alignment import identity_sim3_transform
    from tools.align.state import RUNTIME

    original_models = dict(RUNTIME.model_repository.models)
    original_index = dict(RUNTIME.model_repository.source_path_index)
    original_transforms = dict(RUNTIME.model_repository.display_transforms)
    original_alignments = dict(RUNTIME.model_repository.alignment_transforms)
    RUNTIME.model_repository.models.clear()
    RUNTIME.model_repository.models.update(models)
    RUNTIME.model_repository.source_path_index.clear()
    RUNTIME.model_repository.display_transforms.clear()
    RUNTIME.model_repository.alignment_transforms.clear()
    RUNTIME.model_repository.alignment_transforms.update(
        {model_id: identity_sim3_transform() for model_id in models}
    )
    if transforms is not None:
        RUNTIME.model_repository.display_transforms.update(transforms)
    return original_models, original_index, original_transforms, original_alignments


def _restore_repository_models(
    original_models: dict[str, object],
    original_index: dict[str, str],
    original_transforms: dict[str, object],
    original_alignments: dict[str, object],
) -> None:
    """테스트 전 Runtime 저장소 모델·표시 축·확정 alignment를 정확히 복원합니다."""
    from tools.align.state import RUNTIME

    RUNTIME.model_repository.models.clear()
    RUNTIME.model_repository.models.update(original_models)
    RUNTIME.model_repository.source_path_index.clear()
    RUNTIME.model_repository.source_path_index.update(original_index)
    RUNTIME.model_repository.display_transforms.clear()
    RUNTIME.model_repository.display_transforms.update(original_transforms)
    RUNTIME.model_repository.alignment_transforms.clear()
    RUNTIME.model_repository.alignment_transforms.update(original_alignments)


def test_display_sampling_is_deterministic_and_exactly_bounded() -> None:
    """POINT3D_ID 표시 샘플이 결정적이며 정확한 상한과 원본 인덱스 범위를 지키는지 확인합니다."""
    from tools.align.viewport.display_sampling import build_display_indices

    point_ids = np.asarray(
        [
            (x * 10_007) + (y * 101) + z + 1
            for x in range(6)
            for y in range(5)
            for z in range(4)
        ],
        dtype=np.uint64,
    )
    first = build_display_indices(point_ids, max_points=8)
    second = build_display_indices(point_ids, max_points=8)

    assert np.issubdtype(first.dtype, np.unsignedinteger)
    assert np.array_equal(first, second)
    assert first.size == 8
    assert np.array_equal(first, np.sort(first))
    assert np.unique(first).size == 8
    assert np.all(first < point_ids.size)


def test_display_sampling_chunking_matches_single_chunk_deterministically() -> None:
    """작은 표시 샘플 청크도 단일 청크와 같은 POINT3D_ID 결과를 선택하는지 확인합니다."""
    from tools.align.viewport.display_sampling import build_display_indices

    point_ids = np.asarray(
        [
            ((index * 37) % 100_003) * 1_009 + 17
            for index in range(1_003)
        ],
        dtype=np.uint64,
    )
    single_chunk = build_display_indices(
        point_ids,
        max_points=64,
        chunk_size=point_ids.size,
    )
    small_chunk_first = build_display_indices(
        point_ids,
        max_points=64,
        chunk_size=7,
    )
    small_chunk_second = build_display_indices(
        point_ids,
        max_points=64,
        chunk_size=7,
    )

    assert np.array_equal(small_chunk_first, single_chunk)
    assert np.array_equal(small_chunk_second, single_chunk)


def test_picker_returns_original_id_through_display_lod_and_compares_multi_a() -> None:
    """벡터화 피커가 LOD 배열 인덱스를 원본 POINT3D_ID로 역매핑하는지 확인합니다."""
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.state import RUNTIME
    from tools.align.viewport.picking import pick_nearest_point

    model_a = SimpleNamespace(
        model_id="model-a",
        geometry_revision=1,
        points=_point_table(
            [100, 7_001],
            [(-0.7, 0.0, -1.0), (0.0, 0.0, -1.0)],
            [(1, 2, 3), (4, 5, 6)],
            [1],
        ),
    )
    model_b = SimpleNamespace(
        model_id="model-b",
        geometry_revision=1,
        points=_point_table(
            [7_001],
            [(0.08, 0.0, -2.0)],
            [(7, 8, 9)],
            [0],
        ),
    )
    original_catalog = RUNTIME.model_catalog
    original_models, original_index, original_transforms, original_alignments = _replace_repository_models(
        {"model-a": model_a, "model-b": model_b}
    )
    try:
        RUNTIME.model_catalog = _active_catalog(("model-a", "model-b"))
        region = SimpleNamespace(width=100, height=100)
        region_3d = SimpleNamespace(
            view_matrix=Matrix.Identity(4),
            perspective_matrix=Matrix.Identity(4),
        )
        result = pick_nearest_point(
            ROLE_REFERENCE,
            region,
            region_3d,
            (50.0, 50.0),
            radius=10.0,
        )

        assert result is not None
        assert result.model_id == "model-a"
        assert result.point3d_id == 7_001
        assert result.position == (0.0, 0.0, -1.0)
        assert result.depth == 1.0
    finally:
        RUNTIME.model_catalog = original_catalog
        _restore_repository_models(
            original_models,
            original_index,
            original_transforms,
            original_alignments,
        )


def test_display_axis_keeps_gpu_source_data_and_aligns_picker_marker_and_fit_bounds() -> None:
    """표시 축이 GPU MVP·피킹·마커·라벨·Fit View에만 같은 방식으로 적용되는지 확인합니다."""
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.display_axes import (
        create_model_display_transform,
        display_bounds,
        display_position,
    )
    from tools.align.operators.workspace import _bounds_for_model_ids
    from tools.align.state import RUNTIME
    from tools.align.viewport import renderer
    from tools.align.viewport.labels import _display_label_position
    from tools.align.viewport.picking import pick_nearest_point

    source_position = (0.0, -1.0, 0.0)
    source_minimum = np.asarray((-1.0, -2.0, -3.0), dtype=np.float64)
    source_maximum = np.asarray((4.0, 5.0, 6.0), dtype=np.float64)
    model = SimpleNamespace(
        model_id="axis-model",
        geometry_revision=1,
        bounds_min=source_minimum,
        bounds_max=source_maximum,
        points=_point_table(
            [77],
            [source_position],
            [(10, 20, 30)],
            [0],
        ),
    )
    transform = create_model_display_transform("MAYA", "-Z", "Y")
    original_catalog = RUNTIME.model_catalog
    original_models, original_index, original_transforms, original_alignments = _replace_repository_models(
        {"axis-model": model},
        {"axis-model": transform},
    )
    try:
        RUNTIME.model_catalog = _active_catalog(("axis-model",))
        shown_position = display_position(transform, source_position)
        assert shown_position[2] < 0.0
        region = SimpleNamespace(width=100, height=100)
        region_3d = SimpleNamespace(
            view_matrix=Matrix.Identity(4),
            perspective_matrix=Matrix.Identity(4),
        )
        result = pick_nearest_point(
            ROLE_REFERENCE,
            region,
            region_3d,
            (
                (shown_position[0] * 0.5 + 0.5) * region.width,
                (shown_position[1] * 0.5 + 0.5) * region.height,
            ),
            radius=2.0,
        )
        assert result is not None
        assert result.point3d_id == 77
        assert result.position == source_position
        assert np.allclose(
            renderer._display_marker_position("axis-model", source_position),
            shown_position,
        )
        assert np.allclose(
            _display_label_position("axis-model", source_position),
            shown_position,
        )
        mvp = renderer._model_view_projection_matrix(Matrix.Identity(4), "axis-model")
        assert np.allclose(np.asarray(mvp), transform.matrix_float64)
        batch_positions, _colors, _source_count, _display_count = renderer._batch_vertex_data(model)
        assert tuple(float(value) for value in batch_positions[0]) == source_position
        fit_bounds = _bounds_for_model_ids(["axis-model"])
        assert fit_bounds is not None
        expected_minimum, expected_maximum = display_bounds(
            transform,
            source_minimum,
            source_maximum,
        )
        assert np.allclose(np.asarray(fit_bounds[0]), expected_minimum)
        assert np.allclose(np.asarray(fit_bounds[1]), expected_maximum)
    finally:
        RUNTIME.model_catalog = original_catalog
        _restore_repository_models(
            original_models,
            original_index,
            original_transforms,
            original_alignments,
        )


def test_gpu_batch_key_uses_lod_revision_without_color_mode_rebuild() -> None:
    """GPU 배치가 정규화 RGB를 유지하고 색상 모드 전환으로 재생성되지 않는지 확인합니다."""
    from tools.align.state import RUNTIME
    from tools.align.viewport import renderer

    model = SimpleNamespace(
        model_id="rgb-model",
        geometry_revision=3,
        lod_revision=5,
        points=_point_table(
            [101, 202],
            [(0.0, 0.0, -1.0), (0.5, 0.5, -2.0)],
            [(12, 34, 56), (78, 90, 123)],
            [1],
        ),
    )
    original_batches = dict(RUNTIME.gpu_batches)
    original_shader_factory = renderer._point_shader
    original_batch_factory = renderer.batch_for_shader
    created_attributes: list[dict[str, np.ndarray]] = []
    try:
        RUNTIME.gpu_batches.clear()
        renderer._point_shader = lambda: object()

        def fake_batch_for_shader(_shader, primitive, attributes):
            """GPU 컨텍스트 없이 batch 입력과 재생성 횟수를 기록합니다."""
            assert primitive == "POINTS"
            created_attributes.append(attributes)
            return object()

        renderer.batch_for_shader = fake_batch_for_shader
        first_batch = renderer.prepare_gpu_batch_for_model("rgb-model", model)
        second_batch = renderer.prepare_gpu_batch_for_model("rgb-model", model)
        cache_key = renderer._batch_key(model)

        assert first_batch is second_batch
        assert cache_key[:3] == ("rgb-model", 3, 5)
        assert len(cache_key[3]) == 32
        assert len(created_attributes) == 1
        assert created_attributes[0]["pos"].dtype == np.float32
        assert created_attributes[0]["color"].shape == (1, 4)
        assert np.allclose(
            created_attributes[0]["color"][0],
            np.asarray((78.0 / 255.0, 90.0 / 255.0, 123.0 / 255.0, 1.0)),
        )
        assert (
            "vertex_color.rgb * identifier_color.rgb"
            in renderer._POINT_FRAGMENT_SHADER_SOURCE
        )
        assert (
            "mix(tinted_color, vertex_color, use_colmap_rgb)"
            in renderer._POINT_FRAGMENT_SHADER_SOURCE
        )
    finally:
        renderer._point_shader = original_shader_factory
        renderer.batch_for_shader = original_batch_factory
        RUNTIME.gpu_batches.clear()
        RUNTIME.gpu_batches.update(original_batches)


def test_gpu_batch_key_changes_when_display_indices_change() -> None:
    """동일 모델·형상에서도 표시 LOD 인덱스가 바뀌면 별도 배치 키를 만드는지 확인합니다."""
    from tools.align.viewport import renderer

    common_positions = [(0.0, 0.0, -1.0), (1.0, 0.0, -1.0)]
    common_colors = [(10, 20, 30), (40, 50, 60)]
    first_model = SimpleNamespace(
        model_id="lod-rebuild-model",
        geometry_revision=9,
        points=_point_table([501, 502], common_positions, common_colors, [0]),
    )
    second_model = SimpleNamespace(
        model_id="lod-rebuild-model",
        geometry_revision=9,
        points=_point_table([501, 502], common_positions, common_colors, [1]),
    )

    first_key = renderer._batch_key(first_model)
    second_key = renderer._batch_key(second_model)

    assert first_key[:3] == second_key[:3] == ("lod-rebuild-model", 9, 0)
    assert first_key[3] != second_key[3]
    assert first_key != second_key


def test_real_blender_gpu_batch_accepts_float32_lod_attributes() -> None:
    """Blender GPU 컨텍스트에서 실제 float32 위치·색상 LOD 배치를 만들 수 있는지 확인합니다."""
    import gpu

    from tools.align.state import RUNTIME
    from tools.align.viewport import renderer

    model = SimpleNamespace(
        model_id="gpu-model",
        geometry_revision=1,
        points=_point_table(
            [501, 502],
            [(0.0, 0.0, -1.0), (1.0, 0.0, -1.0)],
            [(255, 0, 0), (0, 255, 0)],
            [0, 1],
        ),
    )
    original_batches = dict(RUNTIME.gpu_batches)
    original_shader = renderer._POINT_SHADER
    try:
        gpu.init()
        RUNTIME.gpu_batches.clear()
        renderer._POINT_SHADER = None
        batch = renderer.prepare_gpu_batch_for_model("gpu-model", model)
        assert batch is RUNTIME.gpu_batches[renderer._batch_key(model)]
    finally:
        RUNTIME.gpu_batches.clear()
        RUNTIME.gpu_batches.update(original_batches)
        renderer._POINT_SHADER = original_shader


def test_picker_source_no_longer_uses_per_point_blender_projection() -> None:
    """실제 피커가 Blender 점별 투영 API에 의존하지 않는지 소스 경계를 확인합니다."""
    from tools.align.viewport import picking

    source_text = Path(picking.__file__).read_text(encoding="utf-8")
    assert "location_3d_to_region_2d" not in source_text
    assert "@ perspective_matrix.T" in source_text


def run_all() -> None:
    """실제 PointTable 렌더·LOD·피킹 회귀를 순서대로 실행합니다."""
    _add_source_path()
    test_display_sampling_is_deterministic_and_exactly_bounded()
    test_display_sampling_chunking_matches_single_chunk_deterministically()
    test_picker_returns_original_id_through_display_lod_and_compares_multi_a()
    test_display_axis_keeps_gpu_source_data_and_aligns_picker_marker_and_fit_bounds()
    test_gpu_batch_key_uses_lod_revision_without_color_mode_rebuild()
    test_gpu_batch_key_changes_when_display_indices_change()
    test_real_blender_gpu_batch_accepts_float32_lod_attributes()
    test_picker_source_no_longer_uses_per_point_blender_projection()
    print("test_viewport_real_data: 통과")


if __name__ == "__main__":
    run_all()
