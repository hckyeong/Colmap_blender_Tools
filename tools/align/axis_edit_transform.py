"""개별 원본 축과 데이터셋 공통 변환을 현재 배치를 보존하면서 합성합니다."""

from dataclasses import dataclass
from typing import Any

import numpy as np

from .display_axes import ModelDisplayTransform, create_model_display_transform, normalize_source_basis
from .geometry_access import bounds_for_model


@dataclass(frozen=True, slots=True)
class DatasetAxisEditBasis:
    """공통 중심 변환과 기존 개별 배치를 보존하는 두 형상의 원본 기준입니다."""
    common: ModelDisplayTransform
    main_basis: tuple[tuple[float, ...], ...] | None
    companion_basis: tuple[tuple[float, ...], ...] | None


def dataset_world_center(runtime: Any, model_id: str, main: ModelDisplayTransform,
                         companion: ModelDisplayTransform | None) -> np.ndarray:
    """대형 배열을 순회하지 않고 존재하는 형상들의 월드 경계 상자를 합친 중심을 계산합니다."""
    repository = runtime.model_repository
    alignment = repository.alignment_transform_for_model_id(model_id).matrix_float64
    geometries = [(repository.models[model_id], main)]
    if companion is not None:
        geometries.append((repository.companion_mesh_for_model_id(model_id), companion))
    bounds = []
    for geometry, transform in geometries:
        minimum, maximum = bounds_for_model(geometry)
        corners = np.array([(x, y, z) for x in (minimum[0], maximum[0])
                            for y in (minimum[1], maximum[1]) for z in (minimum[2], maximum[2])])
        matrix = alignment @ transform.matrix_float64
        world = corners @ matrix[:3, :3].T + matrix[:3, 3]
        if not np.isfinite(world).all():
            raise ValueError("데이터셋 경계 중심에 유한하지 않은 좌표가 있습니다.")
        bounds.append((world.min(axis=0), world.max(axis=0)))
    return np.minimum.reduce([bound[0] for bound in bounds]) * 0.5 + np.maximum.reduce([bound[1] for bound in bounds]) * 0.5


def common_transform_for_geometry(transform: ModelDisplayTransform) -> ModelDisplayTransform:
    """주 형상의 원본 축을 제외한 이동·회전·배율을 데이터셋 공통 좌표로 표현합니다."""
    axis = create_model_display_transform(transform.preset_id, transform.forward_axis, transform.up_axis)
    return create_model_display_transform(
        "BLENDER", "Y", "Z", rotation_xyz_degrees=transform.rotation_xyz_degrees,
        translation_xyz=transform.translation_xyz, scale_xyz=transform.scale_xyz,
        pivot_source=axis.matrix_float64[:3, :3] @ transform.pivot_source,
    )


def _geometry_basis(common: ModelDisplayTransform, transform: ModelDisplayTransform):
    """공통 변환과 원본 축을 제외한 기존 개별 보정을 가역 아핀 기준으로 고정합니다."""
    axis = create_model_display_transform(transform.preset_id, transform.forward_axis, transform.up_axis)
    basis = np.eye(4, dtype=np.float64)
    basis[:3, :3] = np.linalg.solve(common.matrix_float64[:3, :3], transform.matrix_float64[:3, :3]) @ axis.inverse_float64[:3, :3]
    basis[:3, 3] = np.linalg.solve(common.matrix_float64[:3, :3], transform.matrix_float64[:3, 3] - common.matrix_float64[:3, 3])
    return normalize_source_basis(basis)


def prepare_dataset_axis_edit(runtime: Any, model_id: str, main: ModelDisplayTransform,
                              companion: ModelDisplayTransform | None) -> DatasetAxisEditBasis:
    """두 형상의 배치를 유지하고 전체 경계 중심을 공통 회전·배율 기준으로 준비합니다."""
    common = common_transform_for_geometry(main)
    main_basis = _geometry_basis(common, main)
    companion_basis = _geometry_basis(common, companion) if companion is not None else None
    alignment = runtime.model_repository.alignment_transform_for_model_id(model_id).matrix_float64
    center = np.linalg.solve(alignment, np.append(dataset_world_center(runtime, model_id, main, companion), 1.0))
    pivot = (common.inverse_float64 @ center)[:3]
    translation = common.matrix_float64[:3, 3] - (pivot - common.matrix_float64[:3, :3] @ pivot)
    centered = create_model_display_transform(
        "BLENDER", "Y", "Z", rotation_xyz_degrees=common.rotation_xyz_degrees,
        translation_xyz=translation, scale_xyz=common.scale_xyz, pivot_source=pivot,
    )
    return DatasetAxisEditBasis(centered, main_basis, companion_basis)


def compose_dataset_geometry_transform(common: ModelDisplayTransform, preset: str,
                                       forward: str, up: str, geometry_basis: object) -> ModelDisplayTransform:
    """독립 원본 축 앞의 기존 보정을 보존하고 두 형상에 같은 공통 변환을 적용합니다."""
    axis = create_model_display_transform(preset, forward, up)
    basis = np.eye(4) if geometry_basis is None else np.asarray(geometry_basis, dtype=np.float64)
    source_basis = axis.inverse_float64 @ basis @ axis.matrix_float64
    return create_model_display_transform(
        preset, forward, up, rotation_xyz_degrees=common.rotation_xyz_degrees,
        translation_xyz=common.translation_xyz, scale_xyz=common.scale_xyz,
        pivot_source=axis.inverse_float64[:3, :3] @ common.pivot_source,
        source_basis=source_basis,
    )
