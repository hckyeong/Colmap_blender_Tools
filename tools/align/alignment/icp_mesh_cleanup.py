"""원본 형상을 보존하며 정렬 계산에서만 퇴화면과 미참조 정점을 제외합니다."""

from __future__ import annotations

import numpy as np


def prepare_mesh_alignment_geometry(
    positions: np.ndarray, triangles: np.ndarray, *, native_precision: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """제한된 크기의 면 묶음으로 퇴화를 검사하고 유효면 순서를 보존해 정점을 압축합니다."""
    positions = np.asarray(positions)
    triangles = np.asarray(triangles)
    if (positions.ndim != 2 or positions.shape[1:] != (3,)
            or positions.dtype.kind not in "fiu" or len(positions) > np.iinfo(np.uint32).max):
        raise ValueError("정렬 메시 좌표 형식이 올바르지 않습니다.")
    if (triangles.ndim != 2 or triangles.shape[1:] != (3,)
            or triangles.dtype.kind not in "iu"):
        raise ValueError("정렬 메시 삼각면 인덱스 형식이 올바르지 않습니다.")
    block_size = 65_536
    for start in range(0, len(positions), block_size):
        if not np.isfinite(positions[start:start + block_size]).all():
            raise ValueError("정렬 메시 좌표에 유한하지 않은 값이 있습니다.")
    valid = np.zeros(len(triangles), dtype=bool)
    used = np.zeros(len(positions), dtype=bool)
    for start in range(0, len(triangles), block_size):
        faces = triangles[start:start + block_size]
        if np.any(faces < 0) or np.any(faces >= len(positions)):
            raise ValueError("정렬 메시 삼각면 인덱스가 정점 범위를 벗어났습니다.")
        corners = np.asarray(positions[faces], dtype=np.float64)
        if native_precision:
            with np.errstate(over="ignore", invalid="ignore"):
                corners = corners.astype(np.float32).astype(np.float64)
            if not np.isfinite(corners).all():
                raise ValueError("정렬 메시 좌표가 단정도 유한 범위를 벗어났습니다.")
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            first = corners[:, 1] - corners[:, 0]
            second = corners[:, 2] - corners[:, 0]
            if not np.isfinite(first).all() or not np.isfinite(second).all():
                raise ValueError("정렬 메시 변의 좌표 차이가 유한 범위를 벗어났습니다.")
            if native_precision:
                cross = np.cross(first, second)
                keep = np.sum(cross * cross, axis=1) > (
                    np.sum(first * first, axis=1) * np.sum(second * second, axis=1) * 1e-16
                )
            else:
                scale = np.maximum(np.abs(first).max(axis=1), np.abs(second).max(axis=1))
                scale[scale == 0] = 1.0
                cross = np.cross(first / scale[:, None], second / scale[:, None])
                keep = np.abs(cross).max(axis=1) > 32.0 * np.finfo(np.float64).eps
        valid[start:start + len(faces)] = keep
        used[faces[keep]] = True
    if not np.any(valid):
        raise ValueError("정렬 메시의 모든 삼각면이 퇴화하여 유효한 표면이 없습니다.")
    kept_faces = triangles if np.all(valid) else triangles[valid]
    if np.all(used):
        return positions, kept_faces
    mapping = np.empty(len(positions), dtype=np.uint32)
    mapping[used] = np.arange(np.count_nonzero(used), dtype=np.uint32)
    remapped = np.empty(kept_faces.shape, dtype=np.uint32)
    for start in range(0, len(kept_faces), block_size):
        remapped[start:start + block_size] = mapping[kept_faces[start:start + block_size]]
    return positions[used], remapped
