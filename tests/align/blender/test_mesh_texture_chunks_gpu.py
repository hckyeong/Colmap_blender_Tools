"""실제 GPU에서 텍스처 배치 분할 전후의 픽셀과 원본 마커 ID를 검증합니다."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import sys
import traceback
import bpy
import gpu
import numpy as np
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.align.viewport import renderer
from test_mesh_normal_reuse import _make_model
from test_screen_marker_gpu import test_actual_gpu_surfaces, test_public_marker_and_draw_failure, test_textured_original_order


def test_pixel_equivalence():
    """UV가 다른 두 원본 면을 나누어도 텍스처와 역할색 출력의 모든 픽셀이 같습니다."""
    result = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        model = _make_model(root, 2)
        fixture = bpy.data.images.new('분할 텍스처 검사', width=2, height=2, alpha=True)
        fixture.pixels.foreach_set([1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 1])
        fixture.filepath_raw = str(root / 'texture.png')
        fixture.file_format = 'PNG'
        fixture.save()
        bpy.data.images.remove(fixture)
        offscreen = gpu.types.GPUOffScreen(64, 64)
        previous = (gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get(), gpu.state.viewport_get())
        try:
            for budget in (256 * 1024 * 1024, 96):
                with patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', budget):
                    renderer.prepare_gpu_batch_for_model(model.model_id, model)
                surface = renderer.RUNTIME.gpu_batches[renderer._mesh_batch_key(model)]
                assert len(surface.textured_batches) == (1 if budget > 96 else 2)
                frames = []
                with offscreen.bind():
                    gpu.state.viewport_set(0, 0, 64, 64)
                    gpu.state.blend_set('NONE')
                    gpu.state.depth_test_set('LESS_EQUAL')
                    gpu.state.depth_mask_set(True)
                    framebuffer = gpu.state.active_framebuffer_get()
                    shader = renderer._mesh_texture_shader()
                    shader.bind()
                    shader.uniform_float('ModelViewProjectionMatrix', Matrix(((.3, 0, 0, -.6), (0, .2, .2, -.6), (0, 0, 0, 0), (0, 0, 0, 1))))
                    shader.uniform_float('NormalMatrix', Matrix.Identity(3))
                    shader.uniform_float('mesh_color', (0.2, 0.7, 0.4, 1))
                    shader.uniform_float('opacity', 1.0)
                    for textured in (1.0, 0.0):
                        framebuffer.clear(color=(0, 0, 0, 0), depth=1.0)
                        shader.uniform_float('use_texture', textured)
                        for batch in surface.textured_batches:
                            shader.uniform_sampler('diffuse_texture', renderer._MESH_TEXTURE_RESOURCES[batch.texture_path].texture)
                            batch.batch.draw(shader)
                        pixels = np.asarray(framebuffer.read_color(0, 0, 64, 64, 4, 0, 'UBYTE'), dtype=np.uint8).copy()
                        assert np.count_nonzero(pixels) > 0
                        frames.append(pixels)
                result.append(frames)
                renderer.discard_gpu_batch_for_model(model.model_id)
            for before, after in zip(*result, strict=True):
                np.testing.assert_array_equal(before, after)
            assert not renderer._MESH_TEXTURE_RESOURCES
        finally:
            renderer.discard_gpu_batch_for_model(model.model_id)
            offscreen.free()
            gpu.state.blend_set(previous[0])
            gpu.state.depth_test_set(previous[1])
            gpu.state.depth_mask_set(previous[2])
            gpu.state.viewport_set(*previous[3])


def main():
    """격리 GUI 프로세스에서 실제 GPU 검사 결과를 남기고 종료합니다."""
    report = {'passed': False, 'gpu': gpu.platform.renderer_get()}
    try:
        test_pixel_equivalence()
        report['pixel_equivalence_cases'] = 2
        report['surface_cases'] = test_actual_gpu_surfaces()
        test_public_marker_and_draw_failure()
        with patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', 96):
            test_textured_original_order()
        report['split_marker_cases'] = 2
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    destination = ROOT / 'build/verification/texture-buffer-chunks-20261021/gpu-tests.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    sys.stdout.flush()
    os._exit(0 if report['passed'] else 1)


if __name__ == '__main__':
    bpy.app.timers.register(main, first_interval=1.0)
