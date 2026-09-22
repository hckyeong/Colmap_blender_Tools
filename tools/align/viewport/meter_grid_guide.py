"""Align 두 뷰의 월드 XY 평면에 데이터와 독립된 1~10미터 눈금을 그립니다."""

from __future__ import annotations

import blf
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector


_LABELS = tuple(
    (f"{index} m", position)
    for index in range(1, 11)
    for position in ((index, 0.15, 0.01), (0.15, index, 0.01))
)
_SEGMENTS = (((0, 0), (10, 0)), ((0, 0), (0, 10))) + tuple(
    segment
    for index in range(1, 11)
    for segment in (((index, -0.08), (index, 0.08)), ((-0.08, index), (0.08, index)))
)
_LINE_VERTICES = tuple((x, y, 0.01) for segment in _SEGMENTS for x, y in segment)
_LINE_SHADER = None
_LINE_BATCH = None


def meter_in_blender_units(scene) -> float:
    """Scene 단위를 유지하며 1미터의 길이를 구하고 단위 없음은 1로 해석합니다."""
    units = scene.unit_settings
    return 1.0 if units.system == "NONE" else 1.0 / units.scale_length


def draw_meter_grid_lines(context) -> None:
    """재사용하는 작은 GPU 배치로 원점부터 10미터까지 기준선과 눈금을 그립니다."""
    global _LINE_SHADER, _LINE_BATCH
    if not context.space_data.overlay.show_overlays:
        return
    if _LINE_BATCH is None:
        _LINE_SHADER = gpu.shader.from_builtin("UNIFORM_COLOR")
        _LINE_BATCH = batch_for_shader(_LINE_SHADER, "LINES", {"pos": _LINE_VERTICES})
    blend = gpu.state.blend_get()
    depth = gpu.state.depth_test_get()
    depth_mask = gpu.state.depth_mask_get()
    width = gpu.state.line_width_get()
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("NONE")
        gpu.state.depth_mask_set(False)
        gpu.state.line_width_set(1.0)
        with gpu.matrix.push_pop():
            gpu.matrix.multiply_matrix(Matrix.Scale(meter_in_blender_units(context.scene), 4))
            _LINE_SHADER.bind()
            _LINE_SHADER.uniform_float("color", (0.68, 0.72, 0.76, 0.55))
            _LINE_BATCH.draw(_LINE_SHADER)
    finally:
        gpu.state.line_width_set(width)
        gpu.state.depth_mask_set(depth_mask)
        gpu.state.depth_test_set(depth)
        gpu.state.blend_set(blend)


def draw_meter_grid_labels(context, draw_text) -> None:
    """고정 월드 눈금에 화면 방향 글자를 붙이고 시야 밖·겹치는 글자는 생략합니다."""
    if not context.space_data.overlay.show_overlays:
        return
    region = context.region
    region_3d = context.region_data or context.space_data.region_3d
    unit = meter_in_blender_units(context.scene)
    ui_scale = context.preferences.system.ui_scale
    size = 12.0 * ui_scale
    padding = 3.0 * ui_scale
    blf.size(0, size)
    occupied = []
    for text, position in _LABELS:
        clip = region_3d.perspective_matrix @ Vector((*[value * unit for value in position], 1.0))
        if clip.w <= 0 or not -clip.w <= clip.z <= clip.w:
            continue
        x = (1.0 + clip.x / clip.w) * region.width * 0.5 + padding
        y = (1.0 + clip.y / clip.w) * region.height * 0.5 + padding
        width, height = blf.dimensions(0, text)
        if x < padding or x + width > region.width - padding or y < 44 * ui_scale or y + height > region.height - 90 * ui_scale:
            continue
        bounds = (x - padding, y - padding, x + width + padding, y + height + padding)
        if any(bounds[0] < old[2] and bounds[2] > old[0] and bounds[1] < old[3] and bounds[3] > old[1] for old in occupied):
            continue
        occupied.append(bounds)
        draw_text(text, x + ui_scale, y - ui_scale, size, (0.0, 0.0, 0.0, 0.8))
        draw_text(text, x, y, size, (0.82, 0.85, 0.88, 0.95))


def clear_meter_grid_resources() -> None:
    """Align 그리기 핸들러 해제 시 눈금의 GPU 자원 참조도 함께 비웁니다."""
    global _LINE_SHADER, _LINE_BATCH
    _LINE_BATCH = None
    _LINE_SHADER = None
