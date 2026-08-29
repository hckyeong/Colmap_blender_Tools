"""ICP 점 분포 신뢰도와 가중 RMS 평가 회귀를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 루트를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _identity_pairs(count: int) -> np.ndarray:
    """같은 index끼리 대응하는 평가용 source·target 쌍을 만듭니다."""

    indices = np.arange(count, dtype=np.int64)
    return np.column_stack((indices, indices))


def test_few_points_and_zero_mad_are_finite() -> None:
    """k보다 점이 적거나 MAD가 0이어도 신뢰도가 유한한지 확인합니다."""

    from tools.align.alignment.icp_confidence import (
        compute_point_distribution_confidence,
        confidence_from_mean_neighbor_distances,
    )

    few_points = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    few = compute_point_distribution_confidence(few_points, k=24)
    assert few.neighbor_count == 3
    assert np.isfinite(few.weights_float64).all()

    zero_mad = confidence_from_mean_neighbor_distances(
        np.ones(32, dtype=np.float64),
        neighbor_count=24,
    )
    assert zero_mad.log_distance_mad == 0.0
    assert not np.any(zero_mad.isolation_candidates_bool)
    np.testing.assert_allclose(zero_mad.weights_float64, 1.0)


def test_uniform_cloud_keeps_raw_and_weighted_rms_equal() -> None:
    """균일 점군의 같은 잔차에서는 raw와 weighted RMS가 같음을 확인합니다."""

    from tools.align.alignment.icp_confidence import (
        compute_point_distribution_confidence,
        weighted_residual_metrics,
    )

    points = np.asarray(
        [(float(x), float(y), 0.0) for x in range(8) for y in range(8)],
        dtype=np.float64,
    )
    confidence = compute_point_distribution_confidence(points)
    residuals = np.full(points.shape[0], 0.02, dtype=np.float64)
    metrics = weighted_residual_metrics(
        residuals,
        _identity_pairs(points.shape[0]),
        confidence.weights_float64,
        confidence.weights_float64,
        confidence.isolation_candidates_bool,
        confidence.isolation_candidates_bool,
    )
    raw_rms = float(np.sqrt(np.mean(np.square(residuals))))
    assert np.isclose(metrics["weighted_point_to_point_rmse"], raw_rms, atol=1.0e-15)


def test_isolated_floaters_do_not_dominate_weighted_rms() -> None:
    """잘 맞은 중심 표면 주변 부유점이 weighted RMS를 독점하지 않는지 확인합니다."""

    from tools.align.alignment.icp_confidence import (
        compute_point_distribution_confidence,
        weighted_residual_metrics,
    )

    surface = np.asarray(
        [(0.1 * x, 0.1 * y, 0.0) for x in range(12) for y in range(12)],
        dtype=np.float64,
    )
    floaters = np.asarray(
        ((5.0, 0.0, 0.0), (-5.0, 0.0, 0.0), (0.0, 5.0, 0.0), (0.0, -5.0, 0.0)),
        dtype=np.float64,
    )
    points = np.vstack((surface, floaters))
    confidence = compute_point_distribution_confidence(points)
    residuals = np.concatenate(
        (
            np.full(surface.shape[0], 0.01, dtype=np.float64),
            np.full(floaters.shape[0], 0.5, dtype=np.float64),
        )
    )
    target_weights = np.ones(points.shape[0], dtype=np.float64)
    target_candidates = np.zeros(points.shape[0], dtype=bool)
    metrics = weighted_residual_metrics(
        residuals,
        _identity_pairs(points.shape[0]),
        confidence.weights_float64,
        target_weights,
        confidence.isolation_candidates_bool,
        target_candidates,
    )
    raw_rms = float(np.sqrt(np.mean(np.square(residuals))))
    assert confidence.isolation_candidate_count >= floaters.shape[0]
    assert float(metrics["weighted_point_to_point_rmse"]) < raw_rms * 0.6
    assert int(metrics["low_weight_correspondence_count"]) >= floaters.shape[0]


def test_global_misalignment_remains_high() -> None:
    """전체 점군의 실제 오정렬은 점 분포 가중 후에도 숨겨지지 않는지 확인합니다."""

    from tools.align.alignment.icp_confidence import weighted_residual_metrics

    count = 64
    residuals = np.full(count, 0.4, dtype=np.float64)
    weights = np.linspace(0.05, 1.0, count, dtype=np.float64)
    candidates = weights < 0.5
    metrics = weighted_residual_metrics(
        residuals,
        _identity_pairs(count),
        weights,
        weights[::-1].copy(),
        candidates,
        candidates[::-1].copy(),
    )
    assert np.isclose(metrics["weighted_point_to_point_rmse"], 0.4, atol=1.0e-15)


def test_rigid_transform_reuses_identical_point_confidence() -> None:
    """이동·회전 전후의 점별 고립도 신뢰도가 변하지 않는지 확인합니다."""

    from tools.align.alignment.icp_confidence import compute_point_distribution_confidence

    rng = np.random.default_rng(20260821)
    points = rng.normal(size=(96, 3)).astype(np.float64)
    angle = np.deg2rad(37.0)
    rotation = np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    transformed = points @ rotation.T + np.asarray((12.0, -8.0, 4.0), dtype=np.float64)
    original = compute_point_distribution_confidence(points)
    rigid = compute_point_distribution_confidence(transformed)
    np.testing.assert_allclose(
        rigid.weights_float64,
        original.weights_float64,
        rtol=0.0,
        atol=1.0e-6,
    )
    np.testing.assert_array_equal(
        rigid.isolation_candidates_bool,
        original.isolation_candidates_bool,
    )


def test_cache_depends_only_on_geometry_revision() -> None:
    """정렬 변환과 무관하게 캐시를 재사용하고 원본 revision 변경 시 폐기하는지 확인합니다."""

    from tools.align.alignment.icp_confidence import (
        confidence_from_mean_neighbor_distances,
    )
    from tools.align.model_repository import ColmapModelRepository

    repository = ColmapModelRepository()
    model = SimpleNamespace(geometry_revision=3)
    repository.models["fixture"] = model
    confidence = confidence_from_mean_neighbor_distances(
        np.ones(32, dtype=np.float64),
        neighbor_count=24,
    )
    repository.cache_icp_confidence_for_model_id(
        "fixture",
        geometry_revision=3,
        confidence=confidence,
    )
    assert repository.cached_icp_confidence_for_model_id("fixture") is confidence
    model.geometry_revision = 4
    assert repository.cached_icp_confidence_for_model_id("fixture") is None


def run_all() -> None:
    """Blender에서 ICP 분포 가중 평가 회귀를 모두 실행합니다."""

    _add_source_path()
    test_few_points_and_zero_mad_are_finite()
    test_uniform_cloud_keeps_raw_and_weighted_rms_equal()
    test_isolated_floaters_do_not_dominate_weighted_rms()
    test_global_misalignment_remains_high()
    test_rigid_transform_reuses_identical_point_confidence()
    test_cache_depends_only_on_geometry_revision()
    print("test_icp_confidence: 통과")


if __name__ == "__main__":
    run_all()
