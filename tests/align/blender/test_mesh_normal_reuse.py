"""메시 배치의 단일 법선 계산과 기존 표면 배열 결과의 동등성을 검증합니다."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np


def _make_model(root: Path, textured_face_count: int):
    """무텍스처·혼합·전체 텍스처 경로에 사용할 불변 메시를 생성합니다."""
    from tools.align.mesh_io.types import ObjMaterialData, TriangleMeshModelData

    vertices = np.asarray(
        ((0, 0, 0), (1.123456789, 0, 0), (0, 2.987654321, 0), (0, 0, 3), (4, 4, 4)),
        dtype=np.float64,
    )
    triangles = np.asarray(((0, 1, 2), (0, 3, 1)), dtype=np.uint32)
    indices = np.arange(5, dtype=np.uint32)
    extra = {}
    if textured_face_count:
        texture_path = root / "texture.png"
        texture_path.write_bytes(b"texture")
        uv_indices = np.full((2, 3), -1, dtype=np.int32)
        uv_indices[:textured_face_count] = ((0, 1, 2), (3, 2, 1))[:textured_face_count]
        extra = dict(
            texcoords_float64=np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0))),
            texcoord_component_counts_uint8=np.full(4, 2, dtype=np.uint8),
            triangle_texcoord_indices_int32=uv_indices,
            materials=(ObjMaterialData("재질", diffuse_texture_path=texture_path),),
            triangle_material_indices_int32=np.zeros(2, dtype=np.int32),
        )
    return TriangleMeshModelData(
        model_id="법선재사용", display_name="법선재사용", source_path=root / "mesh.obj",
        vertices_float64=vertices, triangles_uint32=triangles,
        registration_vertex_indices=indices, display_vertex_indices=indices,
        bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0), **extra,
    )


def test_mesh_normal_reuse() -> None:
    """직접 호출과 GPU 배치 준비의 배열이 같고 배치당 법선을 한 번 계산하는지 검증합니다."""
    from tools.align.viewport import renderer

    for textured_face_count in (0, 1, 2):
        with TemporaryDirectory() as temporary_directory:
            model = _make_model(Path(temporary_directory), textured_face_count)
            vertices = np.ascontiguousarray(model.vertices_float64, dtype=np.float32)
            triangles = model.triangles_uint32
            expected_normals = renderer._mesh_vertex_normals(vertices, triangles)
            with patch.object(renderer, "_mesh_vertex_normals", wraps=renderer._mesh_vertex_normals) as normal_call:
                expected_solid, expected_groups = renderer._mesh_surface_arrays(model)
                assert normal_call.call_count == 1
                reused_solid, reused_groups = renderer._mesh_surface_arrays(model, normals=expected_normals)
                assert normal_call.call_count == 1
            np.testing.assert_array_equal(expected_solid, reused_solid)
            assert len(expected_groups) == len(reused_groups)
            for expected, reused in zip(expected_groups, reused_groups, strict=True):
                assert expected[0] == reused[0]
                for expected_array, reused_array in zip(expected[1:], reused[1:], strict=True):
                    np.testing.assert_array_equal(expected_array, reused_array)

            captured = []

            def capture_batch(shader, primitive, attributes, *, indices=None):
                """GPU 업로드 직전의 배열과 인덱스를 기록해 실제 그래픽 문맥 의존성을 제거합니다."""
                batch = (shader, primitive, attributes, indices)
                captured.append(batch)
                return batch

            with (
                patch.dict(renderer.RUNTIME.gpu_batches, {}, clear=True),
                patch.object(renderer, "_point_shader", return_value="점"),
                patch.object(renderer, "_mesh_shader", return_value="면"),
                patch.object(renderer, "_mesh_texture_shader", return_value="텍스처"),
                patch.object(renderer, "_acquire_texture_resource") as acquire,
                patch.object(renderer, "batch_for_shader", side_effect=capture_batch),
                patch.object(renderer, "_mesh_vertex_normals", wraps=renderer._mesh_vertex_normals) as normal_call,
            ):
                point_batch = renderer.prepare_gpu_batch_for_model(model.model_id, model)
                assert normal_call.call_count == 1
                assert normal_call.call_args.args[0].dtype == np.float32
                np.testing.assert_array_equal(normal_call.call_args.args[0], vertices)
                np.testing.assert_array_equal(normal_call.call_args.args[1], triangles)
                surface_batches = captured[1:]
                if expected_solid.size:
                    solid = surface_batches.pop(0)
                    np.testing.assert_array_equal(solid[2]["pos"], vertices)
                    np.testing.assert_array_equal(solid[2]["normal"], expected_normals)
                    np.testing.assert_array_equal(solid[3], expected_solid)
                assert len(surface_batches) == len(expected_groups)
                for actual, expected in zip(surface_batches, expected_groups, strict=True):
                    for key, expected_array in zip(("pos", "normal", "uv"), expected[1:], strict=True):
                        np.testing.assert_array_equal(actual[2][key], expected_array)
                assert acquire.call_count == len(expected_groups)
                assert renderer.prepare_gpu_batch_for_model(model.model_id, model) is point_batch
                assert normal_call.call_count == 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    test_mesh_normal_reuse()
    print("메시 법선 재사용 검증 통과: 무텍스처·혼합·전체 텍스처")
