"""삼각형 OBJ 데이터 계층과 공통 등록점 경계를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _write_obj(path: Path, body: str) -> Path:
    """임시 OBJ fixture를 UTF-8 텍스트로 기록하고 경로를 반환합니다."""

    path.write_text(body, encoding="utf-8")
    return path


def test_obj_token_variants_negative_indices_and_comments() -> None:
    """지원하는 face token 네 종류와 음수 인덱스가 같은 원본 정점을 읽는지 확인합니다."""

    from tools.align.mesh_io import read_obj

    with TemporaryDirectory() as temporary_directory:
        path = _write_obj(
            Path(temporary_directory) / "variants.obj",
            """# header
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
vt 0 0
vn 0 0 1
f 1 2 3 # inline comment
f 1/1 2/1 4/1
f 1//1 3//1 4//1
f 1/1/1 2/1/1 3/1/1
f -4 -3 -2
o ignored
g ignored
usemtl ignored
""",
        )
        result = read_obj(path)
    assert result.vertex_count == 4
    assert result.triangle_count == 5
    assert result.texture_coordinate_count == 1
    assert np.array_equal(
        result.texcoords_float64,
        np.asarray(((0.0, 0.0, 0.0),), dtype=np.float64),
    )
    assert np.array_equal(
        result.texcoord_component_counts_uint8,
        np.asarray((2,), dtype=np.uint8),
    )
    assert np.array_equal(
        result.triangle_texcoord_indices_int32,
        np.asarray(
            (
                (-1, -1, -1),
                (0, 0, 0),
                (-1, -1, -1),
                (0, 0, 0),
                (-1, -1, -1),
            ),
            dtype=np.int32,
        ),
    )
    assert np.array_equal(result.registration_vertex_indices, np.arange(4, dtype=np.uint32))
    assert result.vertices_float64.flags.writeable is False
    assert result.triangles_uint32.flags.writeable is False
    assert result.texcoords_float64.flags.writeable is False
    assert result.triangle_texcoord_indices_int32.flags.writeable is False


def test_obj_uv_seams_components_negative_indices_and_partial_face_set() -> None:
    """UV seam·1~3성분 vt·음수 UV 인덱스·면 단위 무UV 혼용을 보존하는지 확인합니다."""

    from tools.align.mesh_io import read_obj

    with TemporaryDirectory() as temporary_directory:
        path = _write_obj(
            Path(temporary_directory) / "uv_seam.obj",
            (
                "v 0 0 0\n"
                "v 1 0 0\n"
                "v 0 1 0\n"
                "v 1 1 0\n"
                "vt 0.25\n"
                "vt 1.0 0.5\n"
                "vt 0.1 0.2 0.3\n"
                "vt 0.9 0.8\n"
                "f 1/1 2/2 3/3\n"
                "f 1/-1 3/-2 4/-3\n"
                "f 1 2 4\n"
            ),
        )
        result = read_obj(path)

    assert result.texture_coordinate_count == 4
    assert result.textured_face_count == 2
    assert np.array_equal(
        result.texcoord_component_counts_uint8,
        np.asarray((1, 2, 3, 2), dtype=np.uint8),
    )
    assert np.allclose(
        result.texcoords_float64,
        np.asarray(
            (
                (0.25, 0.0, 0.0),
                (1.0, 0.5, 0.0),
                (0.1, 0.2, 0.3),
                (0.9, 0.8, 0.0),
            ),
            dtype=np.float64,
        ),
    )
    assert np.array_equal(
        result.triangle_texcoord_indices_int32,
        np.asarray(((0, 1, 2), (3, 2, 1), (-1, -1, -1)), dtype=np.int32),
    )


def test_obj_mtl_diffuse_texture_and_face_materials_are_preserved() -> None:
    """mtllib·usemtl·map_Kd를 해석해 면 재질과 존재하는 원본 이미지 경로를 보존하는지 확인합니다."""

    from tools.align.mesh_io import read_obj

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        texture_root = root / "textures"
        texture_root.mkdir()
        texture_path = texture_root / "albedo image.png"
        texture_path.write_bytes(b"original-texture-bytes")
        (root / "materials.mtl").write_text(
            (
                "newmtl Textured Body\n"
                "Kd 0.8 0.7 0.6\n"
                'map_Kd -s 1 1 1 "textures/albedo image.png"\n'
                "newmtl Plain\n"
                "Kd 0.2 0.3 0.4\n"
            ),
            encoding="utf-8",
        )
        path = _write_obj(
            root / "materials.obj",
            (
                "mtllib materials.mtl\n"
                "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                "vt 0 0\nvt 1 0\nvt 0 1\nvt 1 1\n"
                "usemtl Textured Body\n"
                "f 1/1 2/2 3/3\n"
                "usemtl Plain\n"
                "f 1/4 3/3 4/2\n"
                "usemtl off\n"
                "f 2 4 3\n"
            ),
        )
        result = read_obj(path)
        material_count = result.material_count
        diffuse_texture_count = result.diffuse_texture_count
        material_names = tuple(material.name for material in result.materials)
        resolved_texture_path = result.materials[0].diffuse_texture_path
        texture_available = result.materials[0].has_available_diffuse_texture
        diffuse_color = result.materials[0].diffuse_color
        face_material_indices = result.triangle_material_indices_int32.copy()

    assert material_count == 2
    assert diffuse_texture_count == 1
    assert material_names == ("Textured Body", "Plain")
    assert resolved_texture_path == texture_path.resolve()
    assert texture_available is True
    assert diffuse_color == (0.8, 0.7, 0.6)
    assert np.array_equal(
        face_material_indices,
        np.asarray((0, 1, -1), dtype=np.int32),
    )


def test_obj_invalid_faces_and_coordinates_are_rejected() -> None:
    """잘못된 면·좌표·UV 참조와 면 내부 부분 UV를 원자적으로 거부하는지 확인합니다."""

    from tools.align.mesh_io import ObjReaderError, read_obj

    invalid_bodies = (
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 3 4\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 1 2\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 1 2\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 4\n",
        "v nan 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt nan 0\nf 1/1 2/1 3/1\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nf 1/0 2/1 3/1\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nf 1/2 2/1 3/1\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nf 1/1 2 3/1\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt\nf 1 2 3\n",
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0 0 0\nf 1 2 3\n",
        "usemtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
    )
    with TemporaryDirectory() as temporary_directory:
        for index, body in enumerate(invalid_bodies):
            path = _write_obj(Path(temporary_directory) / f"invalid_{index}.obj", body)
            try:
                read_obj(path)
            except ObjReaderError:
                continue
            raise AssertionError(f"잘못된 OBJ 입력을 허용했습니다: {index}")


def test_obj_import_accepts_faces_beyond_previous_hard_limit() -> None:
    """600,000면 경계를 넘은 OBJ를 읽고 Native 검증이 중복면만 정리하는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    from tools.align.mesh_io import read_obj
    from tools.align.state import RuntimeState

    prefix = "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
    face = "f 1 2 3\n"
    face_count = 600_001
    with TemporaryDirectory() as temporary_directory:
        accepted = _write_obj(
            Path(temporary_directory) / "accepted.obj",
            prefix + face * face_count,
        )
        accepted_result = read_obj(accepted)
        assert accepted_result.triangle_count == face_count
        del accepted_result
        runtime = RuntimeState()
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            mesh_importer.run_mesh_import_job(
                runtime,
                accepted,
                model_id_factory=lambda: "face-limit",
            )
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard
        model = runtime.model_repository.models["face-limit"]
        assert model.original_polygon_count == 1 and model.face_count == 1


def test_obj_reader_advances_in_bounded_steps() -> None:
    """대형 OBJ reader가 한 TIMER 단계에서 파일 전체를 동기 처리하지 않는지 확인합니다."""

    from tools.align.mesh_io import ObjReader

    with TemporaryDirectory() as temporary_directory:
        path = _write_obj(
            Path(temporary_directory) / "incremental.obj",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\n" + "f 1 2 3\n" * 1_000,
        )
        reader = ObjReader(path)
        first_progress = reader.step(time_budget_seconds=0.0)
        assert reader.done is False
        assert 0 < first_progress.processed_lines < 1_003
        while not reader.done:
            reader.step(time_budget_seconds=0.0)
        assert reader.model.triangle_count == 1_000


def test_mesh_import_job_is_atomic_and_geometry_access_is_common() -> None:
    """OBJ job이 메시 Data Layer를 등록하고 ICP용 등록점 ID를 원본 인덱스로 제공하는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    from tools.align.constants import GEOMETRY_KIND_TRIANGLE_MESH, MARKER_POINT_KIND_MESH_VERTEX
    from tools.align.geometry_access import (
        registration_point_ids_for_model,
        registration_positions_for_model,
        source_position_for_registration_id,
    )
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        texture_path = root / "mesh_texture.png"
        texture_path.write_bytes(b"runtime-texture")
        (root / "mesh.mtl").write_text(
            "newmtl MeshMaterial\nKd 1 1 1\nmap_Kd mesh_texture.png\n",
            encoding="utf-8",
        )
        path = _write_obj(
            root / "mesh.obj",
            (
                "mtllib mesh.mtl\nusemtl MeshMaterial\n"
                "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                "vt 0 0\nvt 1 0\nvt 0 1\nvt 1 1\n"
                "f 1/1 2/2 3/3\nf 1/4 3/3 4/2\n"
            ),
        )
        runtime = RuntimeState()
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            job = mesh_importer.run_mesh_import_job(
                runtime,
                path,
                model_id_factory=lambda: "mesh",
                display_name_hint="mesh",
            )
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard
        model = runtime.model_repository.models["mesh"]
        assert job.stage == mesh_importer.MESH_IMPORT_STAGE_COMPLETED
        assert runtime.model_catalog.models["mesh"].geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH
        assert runtime.model_repository.source_metadata["mesh"].geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH
        assert model.texture_coordinate_count == 6
        assert model.textured_face_count == 2
        assert model.has_complete_texture_coordinates is True
        assert model.material_count == 1
        assert model.diffuse_texture_count == 1
        assert model.materials[0].diffuse_texture_path == texture_path.resolve()
        assert np.array_equal(
            model.triangle_material_indices_int32,
            np.asarray((0, 0), dtype=np.int32),
        )
        assert np.array_equal(
            model.triangle_texcoord_indices_int32,
            np.asarray(((0, 1, 2), (3, 4, 5)), dtype=np.int32),
        )
        assert np.allclose(
            model.texcoords_float64[:, :2],
            np.asarray(((0, 0), (1, 0), (0, 1), (1, 1), (0, 1), (1, 0)), dtype=np.float64),
        )
        from tools.align.viewport.renderer import _mesh_surface_arrays

        solid_faces, textured_groups, _ = _mesh_surface_arrays(model)
        assert solid_faces.shape == (0, 3)
        assert len(textured_groups) == 1
        assert textured_groups[0][1].shape == (6, 3)
        assert textured_groups[0][3].shape == (6, 2)
        assert np.array_equal(registration_point_ids_for_model(model), np.asarray((0, 1, 2, 3), dtype=np.uint64))
        assert registration_positions_for_model(model).shape == (4, 3)
        assert np.array_equal(
            source_position_for_registration_id(model, MARKER_POINT_KIND_MESH_VERTEX, 3),
            np.asarray((0.0, 0.0, 1.0), dtype=np.float64),
        )


def test_mesh_display_sample_uses_native_mesh_vertex_indices() -> None:
    """표시 샘플이 Native Mesh에서 실제 Polygon이 참조하는 정점 ID만 사용하는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary_directory:
        path = _write_obj(
            Path(temporary_directory) / "sparse_vertices.obj",
            (
                "v 9 9 9\n"
                "v 0 0 0\n"
                "v 8 8 8\n"
                "v 1 0 0\n"
                "v 0 1 0\n"
                "f 2 4 5\n"
            ),
        )
        runtime = RuntimeState()
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            mesh_importer.run_mesh_import_job(
                runtime,
                path,
                model_id_factory=lambda: "sparse",
                display_vertex_limit=2,
            )
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard
        model = runtime.model_repository.models["sparse"]
        assert model.vertex_count == 3
        assert set(int(value) for value in model.display_vertex_indices).issubset({0, 1, 2})
        assert model.display_vertex_indices.size == 2


def test_mesh_gpu_batch_prepares_surface_and_vertex_overlay() -> None:
    """Blender GPU에서 메시 삼각면 셰이더와 마커 정점 overlay 배치를 함께 생성하는지 확인합니다."""

    import bpy
    import gpu
    from mathutils import Matrix

    if bpy.app.background:
        # 백그라운드 Blender는 GPU 모듈을 초기화하지 않으므로 실제 창·MCP 검증에서 실행합니다.
        return
    from tools.align.mesh_io import TriangleMeshModelData, read_obj
    from tools.align.state import RUNTIME
    from tools.align.viewport.renderer import (
        _MESH_TEXTURE_RESOURCES,
        _MeshSurfaceBatch,
        _mesh_texture_shader,
        discard_gpu_batch_for_model,
        prepare_gpu_batch_for_model,
    )

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        fixture_image = bpy.data.images.new("COLMAP GPU 테스트", width=2, height=2, alpha=True)
        fixture_image.generated_color = (0.2, 0.6, 0.9, 1.0)
        fixture_image.filepath_raw = str(root / "gpu.png")
        fixture_image.file_format = "PNG"
        fixture_image.save()
        bpy.data.images.remove(fixture_image)
        (root / "gpu.mtl").write_text(
            "newmtl Surface\nKd 1 1 1\nmap_Kd gpu.png\n",
            encoding="utf-8",
        )
        path = _write_obj(
            root / "gpu.obj",
            (
                "mtllib gpu.mtl\nusemtl Surface\n"
                "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                "vt 0 0\nvt 1 0\nvt 0 1\nf 1/1 2/2 3/3\n"
            ),
        )
        parsed = read_obj(path)
        model = TriangleMeshModelData(
            model_id="gpu-mesh",
            display_name="gpu-mesh",
            source_path=path,
            vertices_float64=parsed.vertices_float64,
            triangles_uint32=parsed.triangles_uint32,
            registration_vertex_indices=parsed.registration_vertex_indices,
            display_vertex_indices=parsed.registration_vertex_indices,
            bounds_min=parsed.bounds_min,
            bounds_max=parsed.bounds_max,
            texcoords_float64=parsed.texcoords_float64,
            texcoord_component_counts_uint8=parsed.texcoord_component_counts_uint8,
            triangle_texcoord_indices_int32=parsed.triangle_texcoord_indices_int32,
            materials=parsed.materials,
            triangle_material_indices_int32=parsed.triangle_material_indices_int32,
        )
        discard_gpu_batch_for_model(model.model_id)
        try:
            point_batch = prepare_gpu_batch_for_model(model.model_id, model)
            model_keys = [key for key in RUNTIME.gpu_batches if key[0] == model.model_id]
            assert point_batch is not None
            assert len(model_keys) == 2
            assert any("TRIS" in key for key in model_keys)
            surface = next(RUNTIME.gpu_batches[key] for key in model_keys if "TRIS" in key)
            assert isinstance(surface, _MeshSurfaceBatch)
            assert surface.solid_batch is None
            assert len(surface.textured_batches) == 1
            assert (root / "gpu.png").resolve() in _MESH_TEXTURE_RESOURCES
            resource = _MESH_TEXTURE_RESOURCES[(root / "gpu.png").resolve()]
            offscreen = gpu.types.GPUOffScreen(32, 32)
            try:
                with offscreen.bind():
                    gpu.state.viewport_set(0, 0, 32, 32)
                    gpu.state.active_framebuffer_get().clear(
                        color=(0.0, 0.0, 0.0, 0.0),
                        depth=1.0,
                    )
                    shader = _mesh_texture_shader()
                    shader.bind()
                    shader.uniform_float("ModelViewProjectionMatrix", Matrix.Identity(4))
                    shader.uniform_float("NormalMatrix", Matrix.Identity(3))
                    shader.uniform_float("mesh_color", (1.0, 0.0, 0.0, 1.0))
                    shader.uniform_float("opacity", 1.0)
                    shader.uniform_float("use_texture", 1.0)
                    shader.uniform_sampler("diffuse_texture", resource.texture)
                    surface.textured_batches[0].batch.draw(shader)
                    pixels = np.asarray(offscreen.texture_color.read(), dtype=np.uint8).reshape((-1, 4))
                    assert np.any(pixels[:, 2] > pixels[:, 0])
                    gpu.state.active_framebuffer_get().clear(
                        color=(0.0, 0.0, 0.0, 0.0),
                        depth=1.0,
                    )
                    shader.uniform_float("use_texture", 0.0)
                    surface.textured_batches[0].batch.draw(shader)
                    role_pixels = np.asarray(
                        offscreen.texture_color.read(),
                        dtype=np.uint8,
                    ).reshape((-1, 4))
                    assert np.any(role_pixels[:, 0] > role_pixels[:, 2])
            finally:
                offscreen.free()
        finally:
            discard_gpu_batch_for_model(model.model_id)
            assert not _MESH_TEXTURE_RESOURCES


def test_mesh_vertex_normals_are_area_weighted_and_normalized() -> None:
    """OBJ에 법선이 없어도 인접 삼각면에서 부드러운 단위 정점 법선을 만드는지 확인합니다."""

    from tools.align.viewport.renderer import _mesh_vertex_normals

    vertices = np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
            (4.0, 4.0, 4.0),
        ),
        dtype=np.float64,
    )
    triangles = np.asarray(((0, 1, 2), (0, 3, 1)), dtype=np.uint32)
    normals = _mesh_vertex_normals(vertices, triangles)
    diagonal = np.asarray((0.0, 1.0, 1.0), dtype=np.float32) / np.sqrt(2.0)
    assert normals.dtype == np.float32
    assert normals.flags.c_contiguous is True
    assert np.allclose(np.linalg.norm(normals, axis=1), 1.0)
    assert np.allclose(normals[0], diagonal)
    assert np.allclose(normals[1], diagonal)
    assert np.allclose(normals[2], (0.0, 0.0, 1.0))
    assert np.allclose(normals[3], (0.0, 1.0, 0.0))
    assert np.allclose(normals[4], (0.0, 0.0, 1.0))


def test_obj_cancel_leaves_runtime_empty() -> None:
    """OBJ 읽기 취소가 repository·catalog에 부분 등록을 남기지 않는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary_directory:
        path = _write_obj(
            Path(temporary_directory) / "cancel.obj",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        )
        runtime = RuntimeState()
        job = mesh_importer.create_mesh_import_job(path, model_id_factory=lambda: "cancelled")
        job.request_cancel()
        assert job.step(runtime) == "CANCELLED"
        assert runtime.model_repository.models == {}
        assert runtime.model_catalog.models == {}


def test_mesh_models_feed_mixed_icp_snapshot_and_project_v2_roundtrip() -> None:
    """두 OBJ 레이어가 기존 ICP 입력과 현재 프로젝트 저장 형식에 함께 연결되는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    from tools.align.alignment.icp_runtime import create_icp_input_snapshot
    from tools.align.constants import GEOMETRY_KIND_TRIANGLE_MESH, PROJECT_PERSISTENCE_SCHEMA_VERSION
    from tools.align.project_persistence.codec import decode_project_state, encode_project_state
    from tools.align.project_persistence.runtime import build_project_state_snapshot
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary_directory:
        anchor_path = _write_obj(
            Path(temporary_directory) / "anchor.obj",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        )
        moving_path = _write_obj(
            Path(temporary_directory) / "moving.obj",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        )
        runtime = RuntimeState()
        runtime.session_active = True
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            run_anchor = mesh_importer.run_mesh_import_job(
                runtime,
                anchor_path,
                model_id_factory=lambda: "anchor",
                display_name_hint="anchor",
            )
            run_moving = mesh_importer.run_mesh_import_job(
                runtime,
                moving_path,
                model_id_factory=lambda: "moving",
                display_name_hint="moving",
            )
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard

        assert run_anchor.stage == mesh_importer.MESH_IMPORT_STAGE_COMPLETED
        assert run_moving.stage == mesh_importer.MESH_IMPORT_STAGE_COMPLETED
        assert runtime.model_catalog.toggle_alignment_selection(
            "anchor",
            has_marker_draft=False,
            completed_pair_count=0,
        )
        icp_input = create_icp_input_snapshot(runtime)
        assert icp_input.seed.moving_model_id == "moving"
        assert icp_input.reference_model_ids == ("anchor",)
        assert np.array_equal(icp_input.moving_point_ids, np.asarray((0, 1, 2), dtype=np.uint64))
        assert np.array_equal(icp_input.reference_point_ids[0], np.asarray((0, 1, 2), dtype=np.uint64))
        assert runtime.model_catalog.models["anchor"].geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH

        snapshot = build_project_state_snapshot(runtime)
        restored = decode_project_state(encode_project_state(snapshot))
        assert restored.schema_version == PROJECT_PERSISTENCE_SCHEMA_VERSION
        assert tuple(model.source.geometry_kind for model in restored.models) == (
            GEOMETRY_KIND_TRIANGLE_MESH,
            GEOMETRY_KIND_TRIANGLE_MESH,
        )
        assert all(model.source.kind == "OBJ" for model in restored.models)
        assert all(model.source.source_format == "obj" for model in restored.models)


def test_mesh_and_colmap_feed_icp_in_both_directions() -> None:
    """OBJ↔COLMAP 두 방향에서 각 형상의 안정 ID와 좌표가 같은 ICP 입력 경계를 통과하는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    import tools.align.operators.export_model as export_model
    from tools.align.alignment.icp_runtime import create_icp_input_snapshot
    from tools.align.colmap_io.types import ColmapModelData, ImageTable, PointTable
    from tools.align.display_axes import identity_display_transform
    from tools.align.model_repository import ImportedModelSourceMetadata
    from tools.align.state import RuntimeState

    positions = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.1), (0.0, 1.0, -0.1), (1.0, 1.0, 0.2)),
        dtype=np.float64,
    )

    def colmap_model(model_id: str) -> ColmapModelData:
        """혼합 ICP 테스트에 필요한 최소 COLMAP 점군 모델을 생성합니다."""
        point_ids = np.asarray((101, 102, 103, 104), dtype=np.uint64)
        points = PointTable(
            point_ids=point_ids,
            positions=positions,
            colors=np.full((4, 3), 255, dtype=np.uint8),
            errors=np.zeros(4, dtype=np.float64),
            track_offsets=np.zeros(5, dtype=np.uint64),
            track_image_ids=np.empty(0, dtype=np.uint64),
            track_point2d_indices=np.empty(0, dtype=np.uint32),
            display_indices=np.arange(4, dtype=np.uint32),
        )
        images = ImageTable(
            image_records={},
            point2d_offsets=np.asarray((0,), dtype=np.uint64),
            point2d_xy=np.empty((0, 2), dtype=np.float64),
            point2d_point3d_ids=np.empty(0, dtype=np.uint64),
        )
        return ColmapModelData(
            model_id=model_id,
            display_name=model_id,
            source_path=Path.cwd() / "mixed_fixture" / model_id,
            source_format="text",
            cameras={},
            images=images,
            rigs={},
            frames={},
            points=points,
            bounds_min=positions.min(axis=0),
            bounds_max=positions.max(axis=0),
        )

    with TemporaryDirectory() as temporary_directory:
        path = _write_obj(
            Path(temporary_directory) / "mixed.obj",
            "v 0 0 0\nv 1 0 0.1\nv 0 1 -0.1\nv 1 1 0.2\nf 1 2 3\nf 2 4 3\n",
        )
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            mesh_anchor_runtime = RuntimeState()
            mesh_anchor_runtime.session_active = True
            mesh_importer.run_mesh_import_job(
                mesh_anchor_runtime,
                path,
                model_id_factory=lambda: "mesh-anchor",
            )
            colmap_moving = colmap_model("colmap-moving")
            mesh_anchor_runtime.model_repository.add_model(colmap_moving, identity_display_transform())
            mesh_anchor_runtime.model_catalog.add_imported_model(
                "colmap-moving",
                "colmap-moving",
                select_after_import=True,
            )
            assert mesh_anchor_runtime.model_catalog.toggle_alignment_selection(
                "mesh-anchor",
                has_marker_draft=False,
                completed_pair_count=0,
            )
            mesh_to_colmap = create_icp_input_snapshot(mesh_anchor_runtime)
            colmap_display = identity_display_transform()
            mesh_anchor_runtime.model_repository.source_metadata["colmap-moving"] = ImportedModelSourceMetadata(
                selected_root_absolute=str(colmap_moving.source_path),
                sparse_path_absolute=str(colmap_moving.source_path),
                source_format="text",
                source_fingerprint="0" * 64,
                display_point_limit=250_000,
                axis_preset=colmap_display.preset_id,
                axis_forward=colmap_display.forward_axis,
                axis_up=colmap_display.up_axis,
                display_transform_fingerprint=colmap_display.fingerprint,
            )
            mesh_anchor_runtime.model_catalog.promote_model_for_icp_commit("colmap-moving")
            original_export_runtime = export_model.RUNTIME
            export_model.RUNTIME = mesh_anchor_runtime
            try:
                export_snapshot = export_model._build_merge_export_snapshot(
                    Path(temporary_directory) / "export"
                )
            finally:
                export_model.RUNTIME = original_export_runtime

            colmap_anchor_runtime = RuntimeState()
            colmap_anchor_runtime.session_active = True
            colmap_anchor = colmap_model("colmap-anchor")
            colmap_anchor_runtime.model_repository.add_model(colmap_anchor, identity_display_transform())
            colmap_anchor_runtime.model_catalog.add_imported_model(
                "colmap-anchor",
                "colmap-anchor",
                select_after_import=False,
            )
            mesh_importer.run_mesh_import_job(
                colmap_anchor_runtime,
                path,
                model_id_factory=lambda: "mesh-moving",
            )
            assert colmap_anchor_runtime.model_catalog.toggle_alignment_selection(
                "colmap-anchor",
                has_marker_draft=False,
                completed_pair_count=0,
            )
            colmap_to_mesh = create_icp_input_snapshot(colmap_anchor_runtime)
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard

    assert np.array_equal(mesh_to_colmap.moving_point_ids, np.asarray((101, 102, 103, 104), dtype=np.uint64))
    assert np.array_equal(mesh_to_colmap.reference_point_ids[0], np.asarray((0, 1, 2, 3), dtype=np.uint64))
    assert np.array_equal(colmap_to_mesh.moving_point_ids, np.asarray((0, 1, 2, 3), dtype=np.uint64))
    assert np.array_equal(colmap_to_mesh.reference_point_ids[0], np.asarray((101, 102, 103, 104), dtype=np.uint64))
    assert tuple(source.model_id for source in export_snapshot.source_models) == ("colmap-moving",)
    assert export_snapshot.excluded_model_ids == ("mesh-anchor",)
    assert export_snapshot.anchor_model_id == "colmap-moving"


def test_obj_project_restore_roundtrip_reimports_mesh_models() -> None:
    """별도 준비 런타임에서 두 OBJ 원본을 복원한 뒤 같은 메시 계층으로 확정하는지 확인합니다."""

    import tools.align.mesh_importer as mesh_importer
    from tools.align.constants import PROJECT_RESTORE_PHASE_RESTORING
    from tools.align.project_persistence.runtime import (
        _new_restore_staging,
        _preflight_restore_sources,
        _restore_timer,
        build_project_state_snapshot,
    )
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first_path = _write_obj(
            root / "first.obj",
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nf 1/1 2/2 3/3\n",
        )
        second_path = _write_obj(
            root / "second.obj",
            "v 0 0 1\nv 1 0 1\nv 0 1 1\nf 1 2 3\n",
        )
        original_prepare = mesh_importer._prepare_gpu_batch_for_model
        original_discard = mesh_importer._discard_gpu_batch_for_model
        mesh_importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        mesh_importer._discard_gpu_batch_for_model = lambda _model_id: None
        try:
            source_runtime = RuntimeState()
            source_runtime.session_active = True
            mesh_importer.run_mesh_import_job(
                source_runtime,
                first_path,
                model_id_factory=lambda: "first-mesh",
            )
            mesh_importer.run_mesh_import_job(
                source_runtime,
                second_path,
                model_id_factory=lambda: "second-mesh",
            )
            snapshot = build_project_state_snapshot(source_runtime)

            restored_runtime = RuntimeState()
            restored_runtime.session_active = True
            restored_runtime.project_restore.generation = 1
            restored_runtime.project_restore.project_uuid = snapshot.project_uuid
            restored_runtime.project_restore.snapshot = snapshot
            restored_runtime.project_restore.phase = PROJECT_RESTORE_PHASE_RESTORING
            _preflight_restore_sources(restored_runtime, snapshot)
            _new_restore_staging(restored_runtime)
            for _index in range(1_000):
                delay = _restore_timer(
                    restored_runtime,
                    generation=1,
                    project_uuid=snapshot.project_uuid,
                    callback=None,
                )
                if delay is None:
                    break
            else:
                raise AssertionError("OBJ 프로젝트 복원이 제한 tick 안에 끝나지 않았습니다.")
        finally:
            mesh_importer._prepare_gpu_batch_for_model = original_prepare
            mesh_importer._discard_gpu_batch_for_model = original_discard

    assert restored_runtime.project_restore.phase == "IDLE"
    assert tuple(restored_runtime.model_repository.models) == ("first-mesh", "second-mesh")
    assert tuple(record.geometry_kind for record in restored_runtime.model_catalog.ordered_models()) == (
        "TRIANGLE_MESH",
        "TRIANGLE_MESH",
    )
    assert tuple(model.face_count for model in restored_runtime.model_repository.models.values()) == (1, 1)
    restored_first = restored_runtime.model_repository.models["first-mesh"]
    assert restored_first.texture_coordinate_count == 3
    assert np.array_equal(
        restored_first.triangle_texcoord_indices_int32,
        np.asarray(((0, 1, 2),), dtype=np.int32),
    )


def test_obj_operator_success_triggers_imported_role_frame_all() -> None:
    """OBJ 모달 성공 종료가 커밋된 모델 ID로 공통 자동 Frame All을 호출하는지 확인합니다."""

    from tools.align.operators import import_mesh

    original_runtime = import_mesh.RUNTIME
    original_notify = import_mesh.notify_project_state_changed
    original_fit = import_mesh.fit_after_successful_import
    original_report = import_mesh._report_status
    original_redraw = import_mesh.request_redraw
    fit_calls = []
    cleared = []
    fake_runtime = SimpleNamespace(
        import_job=SimpleNamespace(staged_model=SimpleNamespace(model_id="obj-imported")),
        import_timer=None,
        clear_import_job=lambda: cleared.append(True),
    )
    context = SimpleNamespace(
        window_manager=SimpleNamespace(progress_end=lambda: None),
    )
    try:
        import_mesh.RUNTIME = fake_runtime
        import_mesh.notify_project_state_changed = lambda _reason: None
        import_mesh.fit_after_successful_import = (
            lambda received_context, model_id: fit_calls.append((received_context, model_id))
        )
        import_mesh._report_status = lambda *_args, **_kwargs: None
        import_mesh.request_redraw = lambda: None
        result = import_mesh.COLMAPALIGN_OT_import_obj_job._finish(
            SimpleNamespace(),
            context,
            {"FINISHED"},
            "OBJ 가져오기 완료",
            {"INFO"},
        )
        assert result == {"FINISHED"}
        assert fit_calls == [(context, "obj-imported")]
        assert cleared == [True]
    finally:
        import_mesh.request_redraw = original_redraw
        import_mesh._report_status = original_report
        import_mesh.fit_after_successful_import = original_fit
        import_mesh.notify_project_state_changed = original_notify
        import_mesh.RUNTIME = original_runtime


def run_all() -> None:
    """OBJ 확장 회귀 테스트를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_obj_token_variants_negative_indices_and_comments()
    test_obj_uv_seams_components_negative_indices_and_partial_face_set()
    test_obj_mtl_diffuse_texture_and_face_materials_are_preserved()
    test_obj_invalid_faces_and_coordinates_are_rejected()
    test_obj_import_accepts_faces_beyond_previous_hard_limit()
    test_obj_reader_advances_in_bounded_steps()
    test_mesh_import_job_is_atomic_and_geometry_access_is_common()
    test_mesh_display_sample_uses_native_mesh_vertex_indices()
    test_mesh_gpu_batch_prepares_surface_and_vertex_overlay()
    test_mesh_vertex_normals_are_area_weighted_and_normalized()
    test_obj_cancel_leaves_runtime_empty()
    test_mesh_models_feed_mixed_icp_snapshot_and_project_v2_roundtrip()
    test_mesh_and_colmap_feed_icp_in_both_directions()
    test_obj_project_restore_roundtrip_reimports_mesh_models()
    test_obj_operator_success_triggers_imported_role_frame_all()
    print("test_mesh_extension: 통과")


if __name__ == "__main__":
    run_all()
