"""검색 구조 없이 원본 면 전체를 평면 교차·면적 좌표로 검사하는 독립 정확도 기준입니다."""
import numpy as np


def nearest_original_triangle(vertices, triangles, origin, direction, chunk_size=100000):
    """전체 원본 삼각형의 평면 교차와 면적 부호로 가장 앞쪽 적중 면을 찾습니다."""
    best = None
    for start in range(0, len(triangles), chunk_size):
        corners = vertices[triangles[start:start + chunk_size]]
        first, second = corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
        normal = np.cross(first, second)
        normal_squared = np.einsum("ij,ij->i", normal, normal)
        denominator = normal @ direction
        valid = (normal_squared > np.finfo(np.float64).tiny) & (np.abs(denominator) > np.finfo(np.float64).tiny)
        distances = np.full(len(corners), np.inf)
        np.divide(np.einsum("ij,ij->i", normal, corners[:, 0] - origin), denominator, out=distances, where=valid)
        valid &= distances >= 0
        if best is not None:
            valid &= distances < best[0]
        offsets = np.flatnonzero(valid)
        if not offsets.size:
            continue
        n = normal[offsets]
        offset = origin + distances[offsets, None] * direction - corners[offsets, 0]
        first_weight = np.einsum("ij,ij->i", np.cross(offset, second[offsets]), n) / normal_squared[offsets]
        second_weight = np.einsum("ij,ij->i", np.cross(first[offsets], offset), n) / normal_squared[offsets]
        inside = (first_weight >= -1e-12) & (second_weight >= -1e-12) & (first_weight + second_weight <= 1 + 1e-12)
        offsets = offsets[inside]
        if offsets.size:
            index = int(offsets[np.argmin(distances[offsets])])
            distance = float(distances[index])
            best = (distance, start + index, origin + distance * direction)
    return best
