"""COLMAP 원본 보존 표시 축·경계·저장소 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _source_aabb_corners(minimum: np.ndarray, maximum: np.ndarray) -> np.ndarray:
    """표시 축 경계 기대값 비교에 쓸 원본 AABB 여덟 모서리를 만듭니다."""
    return np.asarray(
        [
            (x, y, z)
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        ],
        dtype=np.float64,
    )


def test_presets_match_reference_axis_contract() -> None:
    """기존 프리셋 축을 유지하면서 RC 표준 OBJ의 독립 기본축을 확인합니다."""
    from tools.align.display_axes import (
        AXIS_PRESETS,
        BLENDER_AXIS_PRESET,
        DEFAULT_AXIS_PRESET,
        axes_for_preset,
        create_model_display_transform,
    )

    assert tuple((item.identifier, item.forward_axis, item.up_axis) for item in AXIS_PRESETS) == (
        ("REALITY_CAPTURE", "Z", "-Y"),
        ("REALITY_CAPTURE_OBJ", "Y", "Z"),
        ("BLENDER", "Y", "Z"),
        ("MAYA", "-Z", "Y"),
        ("HOUDINI", "-Z", "Y"),
        ("CUSTOM", None, None),
    )
    assert DEFAULT_AXIS_PRESET == "REALITY_CAPTURE"
    assert axes_for_preset("CUSTOM") is None
    blender_transform = create_model_display_transform(
        BLENDER_AXIS_PRESET,
        "Y",
        "Z",
    )
    assert np.allclose(blender_transform.matrix_float64, np.identity(4))
    assert np.allclose(
        blender_transform.matrix_float64 @ blender_transform.inverse_float64,
        np.identity(4),
    )


def test_custom_axis_never_mutates_source_positions_and_bounds_use_all_corners() -> None:
    """Custom 표시 축이 원본 배열을 보존하고 변환 AABB 여덟 모서리를 반영하는지 확인합니다."""
    from tools.align.display_axes import (
        create_model_display_transform,
        display_bounds,
        display_positions,
        ensure_distinct_axis_pair,
    )

    transform = create_model_display_transform("CUSTOM", "X", "Z")
    source = np.asarray(
        [
            (-2.0, -3.0, -4.0),
            (5.0, 7.0, 11.0),
            (1.0, -1.0, 2.0),
        ],
        dtype=np.float64,
    )
    source_before = source.copy()
    shown = display_positions(transform, source)
    assert np.array_equal(source, source_before)
    restored_homogeneous = np.concatenate(
        (shown, np.ones((shown.shape[0], 1), dtype=np.float64)),
        axis=1,
    ) @ transform.inverse_float64.T
    assert np.allclose(restored_homogeneous[:, :3], source)

    minimum = np.asarray((-2.0, -3.0, -4.0), dtype=np.float64)
    maximum = np.asarray((5.0, 7.0, 11.0), dtype=np.float64)
    displayed_minimum, displayed_maximum = display_bounds(transform, minimum, maximum)
    transformed_corners = display_positions(
        transform,
        _source_aabb_corners(minimum, maximum),
    )
    assert np.allclose(displayed_minimum, transformed_corners.min(axis=0))
    assert np.allclose(displayed_maximum, transformed_corners.max(axis=0))
    assert ensure_distinct_axis_pair("X", "-X", "FORWARD") == ("X", "Z")
    assert ensure_distinct_axis_pair("-Y", "Y", "UP") == ("Z", "Y")


def test_invalid_axis_pair_is_rejected_before_import_job_creation() -> None:
    """같은 기준축 Forward·Up은 파일을 읽기 전에 명확히 거부하는지 확인합니다."""
    from tools.align.importer import ImportJobError, create_import_job

    try:
        create_import_job(
            "C:/axis-test/sparse",
            axis_preset="CUSTOM",
            axis_forward="X",
            axis_up="-X",
        )
    except ImportJobError as error:
        assert "표시 축 설정" in str(error)
        return
    raise AssertionError("같은 기준축 Forward·Up 조합이 거부되지 않았습니다.")


def test_repository_registers_and_removes_model_transform_atomically() -> None:
    """모델·경로 색인·표시 축 변환이 함께 등록되고 함께 제거되는지 확인합니다."""
    from tools.align.display_axes import create_model_display_transform
    from tools.align.model_repository import ColmapModelRepository

    repository = ColmapModelRepository()
    model = SimpleNamespace(
        model_id="axis-repository-model",
        source_path=Path("C:/axis-test/sparse").resolve(),
        points=SimpleNamespace(point_ids=np.asarray((1,), dtype=np.uint64)),
    )
    transform = create_model_display_transform("MAYA", "-Z", "Y")
    repository.add_model(model, transform)
    assert repository.models[model.model_id] is model
    assert repository.display_transform_for_model_id(model.model_id) is transform
    assert repository.remove_model(model.model_id) is model
    assert repository.models == {}
    assert repository.source_path_index == {}
    assert repository.display_transforms == {}


def run_all() -> None:
    """표시 축 단위·저장소 계약 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_presets_match_reference_axis_contract()
    test_custom_axis_never_mutates_source_positions_and_bounds_use_all_corners()
    test_invalid_axis_pair_is_rejected_before_import_job_creation()
    test_repository_registers_and_removes_model_transform_atomically()
    print("test_display_axes: 통과")


if __name__ == "__main__":
    run_all()
