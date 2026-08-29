"""삼각형 OBJ 입력의 하드 제한과 배열 무결성을 검증합니다."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..constants import OBJ_MAX_TRIANGLE_FACES


class MeshValidationError(ValueError):
    """메시 입력이 1차 삼각형 OBJ 계약을 만족하지 않을 때 발생합니다."""


def validate_obj_limits(*, vertex_count: int, triangle_count: int) -> None:
    """OBJ 정점·삼각면 개수를 600,000면 하드 제한과 빈 입력 규칙으로 확인합니다."""
    if isinstance(vertex_count, bool) or int(vertex_count) < 1:
        raise MeshValidationError("OBJ 유효 정점이 없습니다.")
    if isinstance(triangle_count, bool) or int(triangle_count) < 1:
        raise MeshValidationError("OBJ 유효 삼각면이 없습니다.")
    if int(triangle_count) > OBJ_MAX_TRIANGLE_FACES:
        raise MeshValidationError("OBJ 삼각면 수가 600,000개 제한을 초과했습니다.")


def validate_triangle_mesh(vertices: Any, triangles: Any, *, max_faces: int = OBJ_MAX_TRIANGLE_FACES) -> tuple[np.ndarray, np.ndarray]:
    """정점·삼각면 배열을 연속 float64·uint32로 검증하고 읽기 전용 복사본을 반환합니다."""
    try:
        vertex_array = np.ascontiguousarray(vertices, dtype=np.float64)
        triangle_array = np.ascontiguousarray(triangles, dtype=np.uint32)
    except (TypeError, ValueError) as error:
        raise MeshValidationError("OBJ 형상 배열을 읽을 수 없습니다.") from error
    if vertex_array.ndim != 2 or vertex_array.shape[1] != 3 or vertex_array.shape[0] < 1:
        raise MeshValidationError("OBJ 정점 배열은 비어 있지 않은 N×3이어야 합니다.")
    if not np.isfinite(vertex_array).all():
        raise MeshValidationError("OBJ 정점 좌표에 NaN 또는 Infinity가 있습니다.")
    if triangle_array.ndim != 2 or triangle_array.shape[1] != 3 or triangle_array.shape[0] < 1:
        raise MeshValidationError("OBJ 삼각면 배열은 M×3이어야 합니다.")
    if triangle_array.shape[0] > int(max_faces):
        raise MeshValidationError("OBJ 삼각면 수가 600,000개 제한을 초과했습니다.")
    if np.any(triangle_array >= vertex_array.shape[0]):
        raise MeshValidationError("OBJ 삼각면의 정점 인덱스가 범위를 벗어났습니다.")
    if np.any(triangle_array[:, 0] == triangle_array[:, 1]) or np.any(triangle_array[:, 1] == triangle_array[:, 2]) or np.any(triangle_array[:, 0] == triangle_array[:, 2]):
        raise MeshValidationError("OBJ 삼각면은 중복 정점 인덱스를 가질 수 없습니다.")
    vertex_array.setflags(write=False)
    triangle_array.setflags(write=False)
    return vertex_array, triangle_array


__all__ = ("MeshValidationError", "validate_obj_limits", "validate_triangle_mesh")
