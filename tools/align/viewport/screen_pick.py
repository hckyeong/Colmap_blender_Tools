"""작은 화면 ID·깊이 패스로 원본 삼각형 후보를 읽고 GPU 상태를 보존합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import gpu
import numpy as np
from mathutils import Matrix

_SHADER: Any | None = None
_PATCH_SIZE = 3
_PIXEL_STEP = 1.0 / 64.0


@dataclass(frozen=True)
class MeshPickChunk:
    """표시와 위치 버퍼를 공유하는 선택 배치와 원본 삼각형 번호를 보관합니다."""

    batch: Any
    triangle_ids: range | np.ndarray


@dataclass(frozen=True)
class SharedMeshPickBatch:
    """공유 위치 버퍼만 그린 뒤 작은 선택 결과를 원본 삼각형 번호로 복원합니다."""

    chunks: tuple[MeshPickChunk, ...]

    def draw(self, shader: Any) -> None:
        """배치마다 겹치지 않는 정수 번호 구간을 부여해 공유 위치 버퍼를 그립니다."""
        offset = 0
        for chunk in self.chunks:
            # 정수 유니폼의 부호 비트를 보존해 32비트 전체 ID 범위를 지원합니다.
            signed_offset = offset if offset < 2**31 else offset - 2**32
            shader.uniform_int("triangle_offset", signed_offset)
            chunk.batch.draw(shader)
            offset += len(chunk.triangle_ids)

    def original_triangle_ids(self, candidates: tuple[int, ...]) -> tuple[int, ...]:
        """읽어 온 소수의 표시 순서 번호를 불변 원본 삼각형 번호로 변환합니다."""
        result = []
        for candidate in candidates:
            offset = 0
            for chunk in self.chunks:
                local = candidate - offset
                if 0 <= local < len(chunk.triangle_ids):
                    result.append(int(chunk.triangle_ids[local]))
                    break
                offset += len(chunk.triangle_ids)
            else:
                raise ValueError("화면 삼각형 ID에 대응하는 표시 배치를 찾을 수 없습니다.")
        return tuple(sorted(set(result)))


def clear_screen_pick_resources() -> None:
    """파일 교체와 세션 종료 때 현재 그래픽 문맥의 셰이더 참조를 해제합니다."""
    global _SHADER
    _SHADER = None


def _pick_shader() -> Any:
    """2의 24승을 넘는 삼각형 번호도 손실 없이 기록하는 정수 셰이더를 만듭니다."""
    global _SHADER
    if _SHADER is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant("MAT4", "model_view_projection")
        info.push_constant("VEC4", "depth_plane")
        info.push_constant("INT", "triangle_offset")
        info.vertex_in(0, "VEC3", "pos")
        interface = gpu.types.GPUStageInterfaceInfo("marker_pick_depth")
        interface.smooth("FLOAT", "linear_depth")
        info.vertex_out(interface)
        info.fragment_out(0, "UINT", "triangle_id")
        info.depth_write("ANY")
        info.vertex_source(
            "void main() { gl_Position = model_view_projection * vec4(pos, 1.0); "
            "linear_depth = dot(depth_plane, vec4(pos, 1.0)); }"
        )
        info.fragment_source(
            "void main() { triangle_id = uint(triangle_offset) + uint(gl_PrimitiveID) + 1u; "
            "gl_FragDepth = linear_depth; }"
        )
        _SHADER = gpu.shader.create_from_info(info)
    return _SHADER


def _patch_matrix(region: Any, projection: Any, mouse: tuple[float, float]) -> np.ndarray:
    """클릭 주변을 확대해 얇은 면의 픽셀 경계 양쪽 후보도 정밀 교차 검사에 포함합니다."""
    width, height = int(region.width), int(region.height)
    x, y = float(mouse[0]), float(mouse[1])
    matrix = np.asarray(projection, dtype=np.float64)
    if width <= 0 or height <= 0 or matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError("마커 선택에 사용할 뷰포트 투영이 올바르지 않습니다.")
    if not np.isfinite((x, y)).all() or not (0 <= x < width and 0 <= y < height):
        raise ValueError("마커 선택 위치가 뷰포트 범위를 벗어났습니다.")
    crop = np.eye(4, dtype=np.float64)
    span = _PATCH_SIZE * _PIXEL_STEP
    crop[0, 0], crop[1, 1] = width / span, height / span
    crop[0, 3], crop[1, 3] = (width - 2 * x) / span, (height - 2 * y) / span
    return crop @ matrix


def _linear_depth_plane(view_matrix: Any, model_matrix: np.ndarray, bounds: tuple[Any, Any]) -> np.ndarray:
    """모델의 눈 좌표 깊이를 정규화하여 원근 깊이가 1 부근에 몰리는 정밀도 손실을 막습니다."""
    view = np.asarray(view_matrix, dtype=np.float64)
    minimum, maximum = np.asarray(bounds, dtype=np.float64)
    if view.shape != (4, 4) or minimum.shape != (3,) or not np.isfinite((minimum, maximum)).all() or np.any(minimum > maximum) or not np.isfinite(view).all():
        raise ValueError("마커 선택용 깊이 범위가 올바르지 않습니다.")
    plane = -(view @ model_matrix)[2].copy()
    lower = float(plane[3] + np.minimum(plane[:3] * minimum, plane[:3] * maximum).sum())
    upper = float(plane[3] + np.maximum(plane[:3] * minimum, plane[:3] * maximum).sum())
    padding = max((upper - lower) * 0.01, max(abs(lower), abs(upper), 1.0) * 1e-6)
    plane[3] -= lower - padding
    plane /= upper - lower + padding * 2
    if not np.isfinite(plane).all():
        raise ValueError("마커 선택용 선형 깊이를 계산할 수 없습니다.")
    return plane


def screen_triangle_candidates(
    batch: Any, model_matrix: np.ndarray, region: Any, region_3d: Any,
    mouse: tuple[float, float], triangle_count: int, bounds: tuple[Any, Any],
) -> tuple[int, ...]:
    """현재 화면의 유효한 ID·깊이를 읽어 원본 삼각형 후보 번호만 반환합니다."""
    if not (0 < int(triangle_count) < np.iinfo(np.uint32).max):
        raise ValueError("마커 선택용 삼각형 개수가 정수 ID 범위를 벗어났습니다.")
    matrix = np.asarray(model_matrix, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError("마커 선택용 모델 변환이 올바르지 않습니다.")
    projection = _patch_matrix(region, region_3d.perspective_matrix, mouse)
    depth_plane = _linear_depth_plane(region_3d.view_matrix, matrix, bounds)
    shader = _pick_shader()
    ids_texture = gpu.types.GPUTexture((_PATCH_SIZE, _PATCH_SIZE), format="R32UI")
    depth_texture = gpu.types.GPUTexture((_PATCH_SIZE, _PATCH_SIZE), format="DEPTH_COMPONENT32F")
    framebuffer = gpu.types.GPUFrameBuffer(color_slots=ids_texture, depth_slot=depth_texture)
    previous_blend = gpu.state.blend_get()
    previous_depth = gpu.state.depth_test_get()
    previous_mask = gpu.state.depth_mask_get()
    previous_viewport = gpu.state.viewport_get()
    previous_scissor = gpu.state.scissor_get()
    try:
        with framebuffer.bind():
            framebuffer.viewport_set(0, 0, _PATCH_SIZE, _PATCH_SIZE)
            gpu.state.scissor_set(0, 0, _PATCH_SIZE, _PATCH_SIZE)
            gpu.state.blend_set("NONE")
            gpu.state.depth_test_set("LESS_EQUAL")
            gpu.state.depth_mask_set(True)
            framebuffer.clear(color=(0, 0, 0, 0), depth=1.0)
            shader.bind()
            shader.uniform_float("model_view_projection", Matrix((projection @ matrix).tolist()))
            shader.uniform_float("depth_plane", tuple(depth_plane))
            shader.uniform_int("triangle_offset", 0)
            batch.draw(shader)
            ids = np.asarray(framebuffer.read_color(0, 0, _PATCH_SIZE, _PATCH_SIZE, 1, 0, "UINT"), dtype=np.uint32).reshape(-1)
            depths = np.asarray(framebuffer.read_depth(0, 0, _PATCH_SIZE, _PATCH_SIZE), dtype=np.float32).reshape(-1)
    finally:
        gpu.state.blend_set(previous_blend)
        gpu.state.depth_test_set(previous_depth)
        gpu.state.depth_mask_set(previous_mask)
        gpu.state.viewport_set(*previous_viewport)
        gpu.state.scissor_set(*previous_scissor)
    valid = (ids != 0) & np.isfinite(depths) & (depths >= 0) & (depths < 1)
    candidates = np.unique(ids[valid])
    if np.any(candidates > triangle_count):
        raise ValueError("화면에서 읽은 삼각형 ID가 원본 범위를 벗어났습니다.")
    result = tuple(int(value) - 1 for value in candidates)
    if isinstance(batch, SharedMeshPickBatch):
        result = batch.original_triangle_ids(result)
        if any(value < 0 or value >= triangle_count for value in result):
            raise ValueError("변환한 삼각형 ID가 원본 범위를 벗어났습니다.")
    return result
