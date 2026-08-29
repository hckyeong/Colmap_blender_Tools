"""결정적 Data Layers 임시 점군과 모델 카탈로그를 준비합니다."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import random

import bpy
import numpy as np

from ..constants import (
    DEMO_MODEL_1_ID,
    DEMO_MODEL_2_ID,
    DEMO_MODEL_3_ID,
    DEMO_MODEL_4_ID,
    DEMO_MODEL_IDS,
    DEMO_POINT_COUNT,
    LEGACY_DEMO_COLLECTION_NAMES,
    LEGACY_DEMO_OBJECT_NAMES,
    LEGACY_OWNED_DATA_KEY,
)
from ..display_axes import identity_display_transform
from ..state import RUNTIME
from ..colmap_io.types import ColmapModelData, ImageTable, PointTable
from ..model_repository import canonical_source_path


@dataclass(frozen=True)
class DemoPointCloud:
    """렌더링·피킹에 사용하는 모델 ID와 원본 좌표 배열을 보관합니다."""

    model_id: str
    name: str
    point_ids: tuple[int, ...]
    positions: tuple[tuple[float, float, float], ...]
    geometry_revision: int = 1


def _reference_position(index: int, per_plane: int, rng: random.Random) -> tuple[float, float, float]:
    """건물 모서리의 세 평면 중 하나에 놓인 재현 가능한 좌표를 생성합니다."""
    plane = min(index // per_plane, 2)
    u = rng.uniform(-4.5, 4.5)
    v = rng.uniform(-3.5, 3.5)
    noise = rng.uniform(-0.025, 0.025)
    if plane == 0:
        return (noise, u, v)
    if plane == 1:
        return (u, noise, v)
    return (u, v, noise)


def _transform_demo_layer(
    position: tuple[float, float, float],
    *,
    scale: float,
    angle_degrees: float,
    offset: tuple[float, float, float],
) -> tuple[float, float, float]:
    """원본 좌표에 결정적 표시용 변환을 적용해 레이어별 점군을 만듭니다."""
    x, y, z = position
    angle = math.radians(angle_degrees)
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    return (
        scale * (cos_angle * x - sin_angle * y) + offset[0],
        scale * (sin_angle * x + cos_angle * y) + offset[1],
        scale * z + offset[2],
    )


def generate_demo_point_clouds(point_count: int = DEMO_POINT_COUNT) -> dict[str, DemoPointCloud]:
    """고정 seed의 네 모델 ID 점군을 Scene 데이터 없이 생성합니다."""
    if point_count < 3:
        raise ValueError("임시 점군은 평면별로 하나 이상인 3점 이상이어야 합니다.")
    rng = random.Random(20260813)
    per_plane = math.ceil(point_count / 3)
    reference_positions = tuple(
        _reference_position(index, per_plane, rng) for index in range(point_count)
    )
    demo_2_positions = tuple(
        _transform_demo_layer(
            position,
            scale=0.86,
            angle_degrees=27.0,
            offset=(3.2, -1.4, 0.8),
        )
        for position in reference_positions
    )
    demo_3_positions = tuple(
        _transform_demo_layer(
            position,
            scale=1.04,
            angle_degrees=-18.0,
            offset=(-4.0, 1.8, -0.6),
        )
        for position in reference_positions
    )
    demo_4_positions = tuple(
        _transform_demo_layer(
            position,
            scale=0.93,
            angle_degrees=41.0,
            offset=(1.1, 4.2, 1.7),
        )
        for position in reference_positions
    )
    point_ids = tuple(range(point_count))
    return {
        DEMO_MODEL_1_ID: DemoPointCloud(
            model_id=DEMO_MODEL_1_ID,
            name="Demo_1",
            point_ids=point_ids,
            positions=reference_positions,
        ),
        DEMO_MODEL_2_ID: DemoPointCloud(
            model_id=DEMO_MODEL_2_ID,
            name="Demo_2",
            point_ids=point_ids,
            positions=demo_2_positions,
        ),
        DEMO_MODEL_3_ID: DemoPointCloud(
            model_id=DEMO_MODEL_3_ID,
            name="Demo_3",
            point_ids=point_ids,
            positions=demo_3_positions,
        ),
        DEMO_MODEL_4_ID: DemoPointCloud(
            model_id=DEMO_MODEL_4_ID,
            name="Demo_4",
            point_ids=point_ids,
            positions=demo_4_positions,
        ),
    }


def ensure_demo_point_clouds() -> dict[str, DemoPointCloud]:
    """기존 테스트 호출부 호환을 위해 이미 설치된 Demo fixture를 재사용합니다."""
    expected_ids = set(DEMO_MODEL_IDS)
    if (
        set(RUNTIME.point_clouds) == expected_ids
        and set(RUNTIME.model_catalog.models) == expected_ids
        and set(RUNTIME.model_repository.models) == expected_ids
        and set(RUNTIME.model_repository.display_transforms) == expected_ids
    ):
        return RUNTIME.point_clouds
    return install_demo_fixture()


def ensure_demo_model_catalog() -> None:
    """기존 테스트 호출부 호환을 위해 Demo 카탈로그를 명시적으로 구성합니다."""
    if not RUNTIME.model_catalog.models:
        RUNTIME.model_catalog.reset_demo_catalog()


def _demo_point_table(cloud: DemoPointCloud) -> PointTable:
    """기존 tuple Demo 점군을 실제 렌더러와 같은 불변 PointTable로 변환합니다."""
    point_count = len(cloud.point_ids)
    point_ids = np.asarray(cloud.point_ids, dtype=np.uint64)
    positions = np.asarray(cloud.positions, dtype=np.float64)
    colors = np.full((point_count, 3), 180, dtype=np.uint8)
    errors = np.zeros(point_count, dtype=np.float64)
    track_offsets = np.zeros(point_count + 1, dtype=np.uint64)
    return PointTable(
        point_ids=point_ids,
        positions=positions,
        colors=colors,
        errors=errors,
        track_offsets=track_offsets,
        track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint64),
        display_indices=np.arange(point_count, dtype=np.uint32),
    )


def _empty_demo_images() -> ImageTable:
    """카메라·이미지가 없는 Demo fixture의 유효한 빈 ImageTable을 생성합니다."""
    return ImageTable(
        image_records={},
        point2d_offsets=np.zeros(1, dtype=np.uint64),
        point2d_xy=np.empty((0, 2), dtype=np.float64),
        point2d_point3d_ids=np.empty(0, dtype=np.uint64),
    )


def _demo_model_data(cloud: DemoPointCloud) -> ColmapModelData:
    """테스트 전용 tuple 점군을 실제 저장소 계약의 모델 데이터로 포장합니다."""
    points = _demo_point_table(cloud)
    source_path = Path(canonical_source_path(Path(".colmap_align_demo_fixture") / cloud.model_id))
    return ColmapModelData(
        model_id=cloud.model_id,
        display_name=cloud.name,
        source_path=source_path,
        source_format="text",
        cameras={},
        images=_empty_demo_images(),
        rigs={},
        frames={},
        points=points,
        bounds_min=np.min(points.positions, axis=0),
        bounds_max=np.max(points.positions, axis=0),
        geometry_revision=1,
    )


def install_demo_fixture(point_count: int = DEMO_POINT_COUNT) -> dict[str, DemoPointCloud]:
    """테스트가 요청할 때만 Demo 카탈로그·저장소·점군을 Scene 데이터 없이 설치합니다."""
    clouds = generate_demo_point_clouds(point_count=point_count)
    RUNTIME.model_catalog.reset_demo_catalog()
    RUNTIME.model_repository.clear()
    for cloud in clouds.values():
        RUNTIME.model_repository.add_model(
            _demo_model_data(cloud),
            identity_display_transform(),
        )
    RUNTIME.point_clouds = clouds
    RUNTIME.gpu_batches.clear()
    return RUNTIME.point_clouds


def _is_owned_legacy_data(data_block: bpy.types.ID) -> bool:
    """정확한 소유 표식이 참인 이전 버전 데이터만 마이그레이션 대상으로 판별합니다."""
    return data_block.get(LEGACY_OWNED_DATA_KEY, None) is True


def cleanup_legacy_owned_demo_data() -> None:
    """이전 버전의 정확한 소유 Proxy·Collection만 제거하고 사용자 동명 데이터는 보존합니다."""
    for name in LEGACY_DEMO_OBJECT_NAMES:
        legacy_object = bpy.data.objects.get(name)
        if legacy_object is not None and _is_owned_legacy_data(legacy_object):
            bpy.data.objects.remove(legacy_object, do_unlink=True)
    for name in LEGACY_DEMO_COLLECTION_NAMES:
        legacy_collection = bpy.data.collections.get(name)
        if legacy_collection is not None and _is_owned_legacy_data(legacy_collection):
            bpy.data.collections.remove(legacy_collection)


CLASSES: tuple[type, ...] = ()
