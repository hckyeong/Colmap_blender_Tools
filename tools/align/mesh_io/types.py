"""Blender 의존성 없이 삼각형 메시와 OBJ 진행 상태를 보관합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

from ..constants import (
    GEOMETRY_KIND_TRIANGLE_MESH,
    OBJ_MAX_TEXTURE_COORDINATES,
    OBJ_MAX_TRIANGLE_FACES,
)


Float64Array: TypeAlias = npt.NDArray[np.float64]
Int32Array: TypeAlias = npt.NDArray[np.int32]
UInt8Array: TypeAlias = npt.NDArray[np.uint8]
UInt32Array: TypeAlias = npt.NDArray[np.uint32]


def _freeze_array(value: object, dtype: object, shape: tuple[int | None, ...], label: str) -> np.ndarray:
    """입력 배열을 연속 dtype으로 복사하고 형상·유한성·쓰기 금지를 확인합니다."""
    try:
        array = np.ascontiguousarray(value, dtype=dtype)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 배열을 읽을 수 없습니다.") from error
    if array.ndim != len(shape) or any(expected is not None and actual != expected for actual, expected in zip(array.shape, shape, strict=True)):
        raise ValueError(f"{label} 배열 형상이 올바르지 않습니다.")
    if not np.isfinite(array).all():
        raise ValueError(f"{label} 배열에 유한하지 않은 값이 있습니다.")
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class ObjMaterialData:
    """OBJ 재질 이름과 MTL의 diffuse 색상·원본 텍스처 경로를 불변 보관합니다."""

    name: str
    diffuse_color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    diffuse_texture_path: Path | None = None
    source_material_path: Path | None = None

    def __post_init__(self) -> None:
        """재질 이름·색상·선택 경로를 검증하고 절대 경로로 정규화합니다."""

        name = str(self.name).strip()
        if not name or "\n" in name or "\r" in name:
            raise ValueError("OBJ 재질 이름이 비어 있거나 줄바꿈을 포함합니다.")
        try:
            diffuse_color = tuple(float(value) for value in self.diffuse_color)
        except (TypeError, ValueError) as error:
            raise ValueError("OBJ diffuse 색상을 읽을 수 없습니다.") from error
        if len(diffuse_color) != 3 or not np.isfinite(diffuse_color).all():
            raise ValueError("OBJ diffuse 색상은 유한한 RGB 세 값이어야 합니다.")
        if any(value < 0.0 for value in diffuse_color):
            raise ValueError("OBJ diffuse 색상은 음수일 수 없습니다.")
        texture_path = (
            None
            if self.diffuse_texture_path is None
            else Path(self.diffuse_texture_path).expanduser().resolve(strict=False)
        )
        material_path = (
            None
            if self.source_material_path is None
            else Path(self.source_material_path).expanduser().resolve(strict=False)
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "diffuse_color", diffuse_color)
        object.__setattr__(self, "diffuse_texture_path", texture_path)
        object.__setattr__(self, "source_material_path", material_path)

    @property
    def has_available_diffuse_texture(self) -> bool:
        """MTL의 diffuse 텍스처가 현재 일반 파일로 존재하는지 반환합니다."""

        path = self.diffuse_texture_path
        return bool(path is not None and path.is_file())


@dataclass(frozen=True, slots=True)
class TriangleMeshModelData:
    """원본 OBJ 정점·삼각면·face-corner UV와 정렬·표시용 인덱스를 불변 보관합니다."""

    model_id: str
    display_name: str
    source_path: Path
    vertices_float64: Float64Array
    triangles_uint32: UInt32Array
    registration_vertex_indices: UInt32Array
    display_vertex_indices: UInt32Array
    bounds_min: Float64Array
    bounds_max: Float64Array
    geometry_revision: int = 1
    texcoords_float64: Float64Array = field(
        default_factory=lambda: np.empty((0, 3), dtype=np.float64)
    )
    texcoord_component_counts_uint8: UInt8Array = field(
        default_factory=lambda: np.empty((0,), dtype=np.uint8)
    )
    triangle_texcoord_indices_int32: Int32Array = field(
        default_factory=lambda: np.empty((0, 3), dtype=np.int32)
    )
    materials: tuple[ObjMaterialData, ...] = ()
    triangle_material_indices_int32: Int32Array = field(
        default_factory=lambda: np.empty((0,), dtype=np.int32)
    )

    def __post_init__(self) -> None:
        """메시 형상·UV 배열의 dtype·인덱스 범위·경계를 검증하고 읽기 전용으로 고정합니다."""
        model_id = str(self.model_id).strip()
        display_name = str(self.display_name).strip()
        if not model_id:
            raise ValueError("메시 모델 ID가 비어 있습니다.")
        if not display_name:
            raise ValueError("메시 모델 표시 이름이 비어 있습니다.")
        vertices = _freeze_array(self.vertices_float64, np.float64, (None, 3), "OBJ 정점")
        if vertices.shape[0] == 0:
            raise ValueError("OBJ 정점이 비어 있습니다.")
        triangles = _freeze_array(self.triangles_uint32, np.uint32, (None, 3), "OBJ 삼각면")
        if triangles.shape[0] < 1 or triangles.shape[0] > OBJ_MAX_TRIANGLE_FACES:
            raise ValueError("OBJ 삼각면 수는 1개 이상 600,000개 이하여야 합니다.")
        if np.any(triangles >= vertices.shape[0]):
            raise ValueError("OBJ 삼각면의 정점 인덱스가 범위를 벗어났습니다.")
        if np.any(triangles[:, 0] == triangles[:, 1]) or np.any(triangles[:, 1] == triangles[:, 2]) or np.any(triangles[:, 0] == triangles[:, 2]):
            raise ValueError("OBJ 삼각면은 같은 정점 인덱스를 반복할 수 없습니다.")
        registration = _freeze_array(self.registration_vertex_indices, np.uint32, (None,), "등록 정점 인덱스")
        display = _freeze_array(self.display_vertex_indices, np.uint32, (None,), "표시 정점 인덱스")
        if registration.size == 0:
            raise ValueError("등록 정점 인덱스가 비어 있습니다.")
        if np.any(registration >= vertices.shape[0]) or np.any(display >= vertices.shape[0]):
            raise ValueError("정점 인덱스가 OBJ 정점 범위를 벗어났습니다.")
        if registration.size and np.any(registration[1:] <= registration[:-1]):
            raise ValueError("등록 정점 인덱스는 중복 없는 오름차순이어야 합니다.")
        if display.size and np.any(display[1:] <= display[:-1]):
            raise ValueError("표시 정점 인덱스는 중복 없는 오름차순이어야 합니다.")
        bounds_min = _freeze_array(self.bounds_min, np.float64, (3,), "메시 경계 최소값")
        bounds_max = _freeze_array(self.bounds_max, np.float64, (3,), "메시 경계 최대값")
        if np.any(bounds_min > bounds_max):
            raise ValueError("메시 경계 최소값이 최대값보다 큽니다.")
        expected_min = vertices.min(axis=0)
        expected_max = vertices.max(axis=0)
        if not np.allclose(bounds_min, expected_min, rtol=0.0, atol=0.0) or not np.allclose(bounds_max, expected_max, rtol=0.0, atol=0.0):
            raise ValueError("메시 경계가 원본 정점 배열과 일치하지 않습니다.")
        if isinstance(self.geometry_revision, bool) or int(self.geometry_revision) < 0:
            raise ValueError("geometry revision은 음수일 수 없습니다.")
        texcoords = _freeze_array(self.texcoords_float64, np.float64, (None, 3), "OBJ UV 좌표")
        if texcoords.shape[0] > OBJ_MAX_TEXTURE_COORDINATES:
            raise ValueError("OBJ UV 좌표 수가 허용 범위를 초과했습니다.")
        component_counts = _freeze_array(
            self.texcoord_component_counts_uint8,
            np.uint8,
            (None,),
            "OBJ UV 성분 수",
        )
        if component_counts.shape[0] != texcoords.shape[0]:
            raise ValueError("OBJ UV 좌표와 성분 수의 개수가 일치하지 않습니다.")
        if component_counts.size and (
            np.any(component_counts < 1) or np.any(component_counts > 3)
        ):
            raise ValueError("OBJ UV 성분 수는 1~3이어야 합니다.")
        raw_face_texcoords = np.ascontiguousarray(
            self.triangle_texcoord_indices_int32,
            dtype=np.int32,
        )
        if raw_face_texcoords.shape == (0, 3) and texcoords.shape[0] == 0:
            raw_face_texcoords = np.full(triangles.shape, -1, dtype=np.int32)
        face_texcoords = _freeze_array(
            raw_face_texcoords,
            np.int32,
            (triangles.shape[0], 3),
            "OBJ 면 UV 인덱스",
        )
        textured_rows = np.all(face_texcoords >= 0, axis=1)
        untextured_rows = np.all(face_texcoords == -1, axis=1)
        if not np.all(textured_rows | untextured_rows):
            raise ValueError("OBJ 한 면의 세 꼭짓점은 모두 UV가 있거나 모두 없어야 합니다.")
        if np.any(face_texcoords[textured_rows] >= texcoords.shape[0]):
            raise ValueError("OBJ 면 UV 인덱스가 UV 좌표 범위를 벗어났습니다.")
        materials = tuple(self.materials)
        if any(not isinstance(material, ObjMaterialData) for material in materials):
            raise ValueError("OBJ 재질 배열에 지원하지 않는 값이 있습니다.")
        material_names = tuple(material.name for material in materials)
        if len(set(material_names)) != len(material_names):
            raise ValueError("OBJ 재질 이름이 중복되었습니다.")
        raw_face_materials = np.ascontiguousarray(
            self.triangle_material_indices_int32,
            dtype=np.int32,
        )
        if raw_face_materials.shape == (0,) and not materials:
            raw_face_materials = np.full((triangles.shape[0],), -1, dtype=np.int32)
        face_materials = _freeze_array(
            raw_face_materials,
            np.int32,
            (triangles.shape[0],),
            "OBJ 면 재질 인덱스",
        )
        if np.any(face_materials < -1) or np.any(face_materials >= len(materials)):
            raise ValueError("OBJ 면 재질 인덱스가 재질 배열 범위를 벗어났습니다.")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "vertices_float64", vertices)
        object.__setattr__(self, "triangles_uint32", triangles)
        object.__setattr__(self, "registration_vertex_indices", registration)
        object.__setattr__(self, "display_vertex_indices", display)
        object.__setattr__(self, "bounds_min", bounds_min)
        object.__setattr__(self, "bounds_max", bounds_max)
        object.__setattr__(self, "geometry_revision", int(self.geometry_revision))
        object.__setattr__(self, "texcoords_float64", texcoords)
        object.__setattr__(self, "texcoord_component_counts_uint8", component_counts)
        object.__setattr__(self, "triangle_texcoord_indices_int32", face_texcoords)
        object.__setattr__(self, "materials", materials)
        object.__setattr__(self, "triangle_material_indices_int32", face_materials)

    @property
    def geometry_kind(self) -> str:
        """Repository가 모델 종류를 분기할 때 사용하는 고정 형상 종류를 반환합니다."""
        return GEOMETRY_KIND_TRIANGLE_MESH

    @property
    def source_format(self) -> str:
        """저장·복원 경계에서 OBJ source 형식을 반환합니다."""
        return "obj"

    @property
    def vertices(self) -> Float64Array:
        """호출부가 짧은 이름으로 원본 정점 배열을 조회할 수 있게 반환합니다."""
        return self.vertices_float64

    @property
    def triangles(self) -> UInt32Array:
        """호출부가 짧은 이름으로 원본 삼각면 배열을 조회할 수 있게 반환합니다."""
        return self.triangles_uint32

    @property
    def face_count(self) -> int:
        """OBJ 원본 삼각면 개수를 반환합니다."""
        return int(self.triangles_uint32.shape[0])

    @property
    def vertex_count(self) -> int:
        """OBJ 원본 정점 개수를 반환합니다."""
        return int(self.vertices_float64.shape[0])

    @property
    def texture_coordinate_count(self) -> int:
        """OBJ 원본 UV 좌표 개수를 반환합니다."""
        return int(self.texcoords_float64.shape[0])

    @property
    def textured_face_count(self) -> int:
        """세 꼭짓점에 UV가 모두 연결된 삼각면 개수를 반환합니다."""
        return int(np.count_nonzero(np.all(self.triangle_texcoord_indices_int32 >= 0, axis=1)))

    @property
    def has_texture_coordinates(self) -> bool:
        """원본 OBJ에 UV 좌표가 하나 이상 있는지 반환합니다."""
        return self.texture_coordinate_count > 0

    @property
    def has_complete_texture_coordinates(self) -> bool:
        """모든 삼각면에 UV가 연결되어 있는지 반환합니다."""
        return self.face_count > 0 and self.textured_face_count == self.face_count

    @property
    def material_count(self) -> int:
        """OBJ 면에서 참조하는 재질 정의 개수를 반환합니다."""

        return len(self.materials)

    @property
    def diffuse_texture_count(self) -> int:
        """현재 파일로 존재하는 고유 diffuse 텍스처 개수를 반환합니다."""

        return len(
            {
                material.diffuse_texture_path
                for material in self.materials
                if material.has_available_diffuse_texture
            }
        )


@dataclass(frozen=True, slots=True)
class MeshImportProgress:
    """OBJ 줄·정점·UV·삼각면 처리량을 TIMER UI에 전달합니다."""

    stage: str
    processed_lines: int = 0
    vertex_count: int = 0
    texture_coordinate_count: int = 0
    triangle_count: int = 0
    fraction: float = 0.0
    message: str = ""


__all__ = ("MeshImportProgress", "ObjMaterialData", "TriangleMeshModelData")
