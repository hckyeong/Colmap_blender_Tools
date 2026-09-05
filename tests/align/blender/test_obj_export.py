"""정렬된 폴리곤 Data Layer의 증분 OBJ Export 계약을 검증합니다."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np


def _add_source_path() -> None:
    """저장소 src를 Blender 테스트 import 경로에 추가합니다."""

    source_root = Path(__file__).resolve().parents[3]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))


def _snapshot(
    root: Path,
    model_id: str,
    matrix: np.ndarray,
    *,
    vertex_count: int = 4,
    face_count: int = 2,
    sequence: int = 1,
    with_uv: bool = False,
    with_texture: bool = False,
):
    """writer·job 테스트에 쓸 선택적 UV·diffuse 텍스처 포함 메시 snapshot을 만듭니다."""

    from tools.align.mesh_io import ObjMaterialData
    from tools.align.mesh_io.obj_writer import ObjExportModelSnapshot, canonical_obj_filename

    if with_texture:
        with_uv = True

    base_vertices = np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    if vertex_count <= base_vertices.shape[0]:
        vertices = base_vertices[:vertex_count]
    else:
        extra = np.zeros((vertex_count - base_vertices.shape[0], 3), dtype=np.float64)
        extra[:, 0] = np.arange(extra.shape[0], dtype=np.float64) + 2.0
        vertices = np.vstack((base_vertices, extra))
    triangle_pattern = np.asarray(((0, 1, 2), (0, 2, 3)), dtype=np.uint32)
    triangles = np.tile(triangle_pattern, ((face_count + 1) // 2, 1))[:face_count]
    texcoords = np.asarray(
        ((0.25, 0.0, 0.0), (1.0, 0.5, 0.0), (0.1, 0.2, 0.3), (0.9, 0.8, 0.0)),
        dtype=np.float64,
    ) if with_uv else np.empty((0, 3), dtype=np.float64)
    component_counts = (
        np.asarray((1, 2, 3, 2), dtype=np.uint8)
        if with_uv
        else np.empty((0,), dtype=np.uint8)
    )
    uv_pattern = np.asarray(((0, 1, 2), (3, 2, 1)), dtype=np.int32)
    face_texcoords = (
        np.tile(uv_pattern, ((face_count + 1) // 2, 1))[:face_count]
        if with_uv
        else np.full((face_count, 3), -1, dtype=np.int32)
    )
    source_path = root / f"{model_id}.obj"
    source_path.write_text("# source fixture\n", encoding="utf-8")
    materials = ()
    face_materials = np.full((face_count,), -1, dtype=np.int32)
    if with_texture:
        import bpy

        texture_path = root / f"{model_id} texture.png"
        fixture_image = bpy.data.images.new(
            f"OBJ Export 테스트 {model_id}",
            width=2,
            height=2,
            alpha=True,
        )
        fixture_image.generated_color = (0.25, 0.5, 0.75, 1.0)
        fixture_image.filepath_raw = str(texture_path)
        fixture_image.file_format = "PNG"
        fixture_image.save()
        bpy.data.images.remove(fixture_image)
        materials = (
            ObjMaterialData(
                name=f"재질 {model_id}",
                diffuse_color=(0.25, 0.5, 0.75),
                diffuse_texture_path=texture_path,
            ),
        )
        face_materials[:] = 0
    return ObjExportModelSnapshot(
        model_id=model_id,
        display_name=f"레이어:{model_id}",
        source_path=source_path,
        output_filename=canonical_obj_filename(sequence, f"레이어:{model_id}", model_id),
        load_order=sequence,
        geometry_revision=1,
        vertex_count=int(vertices.shape[0]),
        face_count=int(triangles.shape[0]),
        source_fingerprint=f"source-{model_id}",
        display_fingerprint=f"display-{model_id}",
        alignment_fingerprint=f"alignment-{model_id}",
        project_world_matrix_float64=matrix,
        vertices_float64=vertices,
        triangles_uint32=triangles,
        texcoords_float64=texcoords,
        texcoord_component_counts_uint8=component_counts,
        triangle_texcoord_indices_int32=face_texcoords,
        materials=materials,
        triangle_material_indices_int32=face_materials,
    )


def _finish_job(job, *, max_steps: int = 10_000, time_budget_seconds: float = 0.0) -> None:
    """지정 시간 예산 tick을 반복해 job을 finalize 준비 상태까지 진행합니다."""

    for _index in range(max_steps):
        job.step(time_budget_seconds)
        if job.status != "RUNNING":
            return
    raise AssertionError("OBJ Export job이 제한 tick 안에 끝나지 않았습니다.")


def _embedded_polygon_runtime(root: Path, model_id: str = "embedded-polygons"):
    """Quad·N-gon·UV seam·면별 재질을 가진 비연결 내장 Mesh Runtime을 만듭니다."""

    import bpy

    from tools.align.display_axes import identity_display_transform
    from tools.align.mesh_io.embedded_obj_store import (
        build_triangle_proxy_from_embedded_mesh,
        commit_embedded_obj_mesh,
    )
    from tools.align.state import RuntimeState

    source_path = Path(os.path.normcase(os.path.normpath(str(root / f"{model_id}.obj"))))
    source_path.write_text("# 최초 Import 출처\n", encoding="utf-8")
    mesh = bpy.data.meshes.new(f"{model_id}-staging")
    vertices = (
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (2.0, 2.0, 0.0),
        (0.0, 2.0, 0.0),
        (-1.0, 2.5, 0.0),
        (-2.0, 1.0, 0.0),
        (-1.0, 0.0, 0.0),
    )
    faces = ((0, 1, 2, 3), (0, 3, 4, 5, 6))
    mesh.from_pydata(vertices, (), faces)
    mesh.update()
    uv_values = (
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
        (0.0, 1.0),
        (0.35, 0.45),
        (0.2, 0.8),
        (0.0, 1.0),
        (-0.2, 0.5),
        (0.0, 0.0),
    )
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop_index, uv in enumerate(uv_values):
        uv_layer.uv[loop_index].vector = uv
    materials = (
        bpy.data.materials.new(f"{model_id}-mat-a"),
        bpy.data.materials.new(f"{model_id}-mat-b"),
    )
    materials[0].diffuse_color = (0.2, 0.4, 0.6, 1.0)
    materials[1].diffuse_color = (0.7, 0.3, 0.1, 1.0)
    materials[0]["colmap_align_source_name"] = "원본 재질 A"
    materials[1]["colmap_align_source_name"] = "원본 재질 B"
    for material in materials:
        mesh.materials.append(material)
    mesh.polygons[0].material_index = 0
    mesh.polygons[1].material_index = 1
    asset_uuid = commit_embedded_obj_mesh(
        mesh,
        model_id=model_id,
        source_path=source_path,
    )
    model = build_triangle_proxy_from_embedded_mesh(
        mesh,
        model_id=model_id,
        display_name=model_id,
        source_path=source_path,
    )
    runtime = RuntimeState()
    runtime.model_repository.add_model(model, identity_display_transform())
    runtime.model_catalog.add_imported_model(
        model_id,
        model_id,
        select_after_import=False,
        geometry_kind="TRIANGLE_MESH",
    )
    return runtime, mesh, materials, source_path, asset_uuid


def _remove_embedded_fixture(mesh, materials) -> None:
    """테스트가 만든 내장 Mesh와 재질 Data Block만 제거합니다."""

    import bpy

    mesh.use_fake_user = False
    bpy.data.meshes.remove(mesh)
    for material in materials:
        if material.name in bpy.data.materials:
            bpy.data.materials.remove(material)


def test_obj_export_filename_uses_only_model_display_name() -> None:
    """Export OBJ 파일명이 순번·ID 지문 없이 모델 표시 이름만 사용하는지 확인합니다."""

    from tools.align.mesh_io.obj_writer import canonical_obj_filename

    filename = canonical_obj_filename(
        7,
        "Gilt-bronze Pensive Maitreya Bodhisattva 78_low poly",
        "stable-model-id",
    )
    assert filename == "Gilt-bronze Pensive Maitreya Bodhisattva 78_low poly.obj"
    assert "0007" not in filename
    assert "stable-model-id" not in filename


def test_obj_writer_bakes_transform_and_reflection_winding() -> None:
    """Project World 변환과 reflection face winding을 canonical OBJ에 정확히 기록하는지 확인합니다."""

    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io.obj_writer import validate_obj_roundtrip, write_obj_incremental

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        matrix = np.asarray(
            (
                (-2.0, 0.0, 0.0, 10.0),
                (0.0, 2.0, 0.0, 20.0),
                (0.0, 0.0, 2.0, 30.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )
        snapshot = _snapshot(root, "reflection", matrix, with_uv=True)
        output_path = root / snapshot.output_filename
        validation = write_obj_incremental(
            output_path,
            snapshot,
            vertex_chunk_size=1,
            face_chunk_size=1,
        )
        parsed = read_obj(output_path)
        assert validation.vertex_count == 4
        assert validation.face_count == 2
        assert validation.texture_coordinate_count == 4
        assert validation.textured_face_count == 2
        assert validation.uv_coverage == 1.0
        assert np.allclose(parsed.vertices_float64[1], (8.0, 20.0, 30.0))
        assert np.array_equal(parsed.triangles_uint32[0], (0, 2, 1))
        assert np.array_equal(parsed.triangle_texcoord_indices_int32[0], (0, 2, 1))
        assert np.array_equal(parsed.triangle_texcoord_indices_int32[1], (3, 1, 2))
        assert np.array_equal(
            parsed.texcoord_component_counts_uint8,
            np.asarray((1, 2, 3, 2), dtype=np.uint8),
        )
        assert validate_obj_roundtrip(output_path, snapshot).output_sha256 == validation.output_sha256


def test_obj_export_job_is_incremental_atomic_and_reported() -> None:
    """여러 레이어를 tick별로 기록·검증하고 후보를 최종 폴더로 승격하는지 확인합니다."""

    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        identity = np.eye(4, dtype=np.float64)
        large = _snapshot(root, "large", identity, vertex_count=9_000, face_count=9_000, sequence=1)
        second_matrix = identity.copy()
        second_matrix[:3, 3] = (3.0, 4.0, 5.0)
        second = _snapshot(root, "second", second_matrix, sequence=2, with_texture=True)
        output_root = root / "aligned_obj"
        job = ObjExportJob(
            output_root,
            (large, second),
            overwrite_existing_output=False,
            job_id="incremental",
        )
        job.step(0.0)
        job.step(0.0)
        job.step(0.0)
        assert job.status == "RUNNING"
        assert 0 < job.written_vertex_count <= 4096
        assert output_root.exists() is False
        assert job.candidate_root.is_dir()
        _finish_job(job)
        assert job.is_ready_to_finalize
        result = job.finalize()
        assert result.output_root == output_root
        assert result.model_count == 2
        assert result.vertex_count == 9_004
        assert result.face_count == 9_002
        assert job.candidate_root.exists() is False
        report = json.loads((output_root / "obj_export_report.json").read_text(encoding="utf-8"))
        assert report["coordinate_space"] == "PROJECT_WORLD"
        assert report["schema_version"] == 3
        assert report["model_count"] == 2
        assert report["total_vertex_count"] == 9_004
        assert report["total_texture_coordinate_count"] == 4
        assert report["total_textured_face_count"] == 2
        assert report["total_material_count"] == 1
        assert report["total_diffuse_texture_count"] == 1
        assert report["total_face_count"] == 9_002
        assert tuple(item["model_id"] for item in report["models"]) == ("large", "second")
        assert report["models"][1]["uv_coverage"] == 1.0
        assert report["models"][1]["material_count"] == 1
        assert len(report["models"][1]["copied_textures"]) == 1


def test_obj_export_copies_original_texture_and_generates_mtl() -> None:
    """생성 MTL이 복사 이미지를 참조하고 출력 이미지 바이트가 원본과 같은지 확인합니다."""

    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io.obj_writer import write_obj_incremental

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        snapshot = _snapshot(
            root,
            "textured model",
            np.eye(4, dtype=np.float64),
            with_texture=True,
        )
        output_path = root / snapshot.output_filename
        validation = write_obj_incremental(output_path, snapshot, face_chunk_size=1)
        material_path = root / snapshot.material_library_filename
        asset = snapshot.texture_assets[0]
        copied_texture = root / asset.output_relative_path
        parsed = read_obj(output_path)
        assert material_path.is_file()
        assert copied_texture.read_bytes() == asset.source_path.read_bytes()
        assert asset.output_relative_path == Path(asset.source_path.name)
        assert not tuple(path for path in root.iterdir() if path.name.endswith("_textures"))
        assert parsed.material_count == 1
        assert parsed.diffuse_texture_count == 1
        assert np.array_equal(parsed.triangle_material_indices_int32, (0, 0))
        assert parsed.materials[0].diffuse_texture_path == copied_texture.resolve(strict=False)
        assert validation.material_count == 1
        assert validation.diffuse_texture_count == 1
        assert validation.copied_texture_sha256[0][0] == asset.output_relative_path.as_posix()


def test_obj_export_shares_same_flat_texture_across_models() -> None:
    """여러 모델이 같은 원본 이미지를 쓰면 Export 루트에 한 번만 복사하는지 확인합니다."""

    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first = _snapshot(root, "first-flat", np.eye(4), sequence=1, with_texture=True)
        second_base = _snapshot(root, "second-flat", np.eye(4), sequence=2, with_uv=True)
        second = replace(
            second_base,
            materials=first.materials,
            triangle_material_indices_int32=np.zeros((second_base.face_count,), dtype=np.int32),
        )
        job = ObjExportJob(
            root / "aligned_obj",
            (first, second),
            overwrite_existing_output=False,
            job_id="shared-flat-texture",
        )
        _finish_job(job)
        result = job.finalize()
        texture_name = first.texture_assets[0].output_relative_path.name
        assert (result.output_root / texture_name).is_file()
        assert len(tuple(result.output_root.glob("*.png"))) == 1
        assert not tuple(result.output_root.glob("*_textures"))


def test_obj_export_rejects_different_flat_textures_with_same_name() -> None:
    """서로 다른 원본 이미지의 basename이 같으면 덮어쓰기 전에 충돌 원인을 표시하는지 확인합니다."""

    from tools.align.mesh_io import ObjMaterialData
    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first_base = _snapshot(root, "first-collision", np.eye(4), sequence=1, with_uv=True)
        second_base = _snapshot(root, "second-collision", np.eye(4), sequence=2, with_uv=True)
        texture_paths = []
        for directory_name, payload in (("a", b"first"), ("b", b"second")):
            directory = root / directory_name
            directory.mkdir()
            texture_path = directory / "albedo.png"
            texture_path.write_bytes(payload)
            texture_paths.append(texture_path)
        snapshots = tuple(
            replace(
                base,
                materials=(
                    ObjMaterialData(
                        name=f"Material-{index}",
                        diffuse_texture_path=texture_paths[index],
                    ),
                ),
                triangle_material_indices_int32=np.zeros((base.face_count,), dtype=np.int32),
            )
            for index, base in enumerate((first_base, second_base))
        )
        try:
            ObjExportJob(
                root / "aligned_obj",
                snapshots,
                overwrite_existing_output=False,
                job_id="flat-texture-collision",
            )
        except ValueError as error:
            message = str(error)
        else:
            raise AssertionError("같은 basename의 서로 다른 텍스처 충돌을 허용했습니다.")
        assert "서로 다른 원본 텍스처" in message
        assert "albedo.png" in message


def test_obj_export_rejects_duplicate_model_display_names() -> None:
    """모델 표시 이름이 같으면 동일 OBJ를 덮어쓰기 전에 명확하게 거부하는지 확인합니다."""

    from tools.align.mesh_io.obj_writer import canonical_obj_filename
    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        filename = canonical_obj_filename(1, "동일 모델", "first")
        first = replace(
            _snapshot(root, "duplicate-first", np.eye(4), sequence=1),
            display_name="동일 모델",
            output_filename=filename,
        )
        second = replace(
            _snapshot(root, "duplicate-second", np.eye(4), sequence=2),
            display_name="동일 모델",
            output_filename=filename,
        )
        try:
            ObjExportJob(
                root / "aligned_obj",
                (first, second),
                overwrite_existing_output=False,
                job_id="duplicate-model-name",
            )
        except ValueError as error:
            message = str(error)
        else:
            raise AssertionError("같은 모델 표시 이름의 OBJ 덮어쓰기를 허용했습니다.")
        assert "모델 이름이 중복" in message
        assert "고유하게 변경" in message


def test_obj_export_preserves_partial_uv_face_set() -> None:
    """UV가 있는 면과 없는 면을 섞어 내보내도 면 단위 연결과 Coverage를 보존하는지 확인합니다."""

    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io.obj_writer import write_obj_incremental

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        snapshot = replace(
            _snapshot(root, "partial-uv", np.eye(4, dtype=np.float64), with_uv=True),
            triangle_texcoord_indices_int32=np.asarray(
                ((0, 1, 2), (-1, -1, -1)),
                dtype=np.int32,
            ),
        )
        output_path = root / snapshot.output_filename
        validation = write_obj_incremental(output_path, snapshot)
        parsed = read_obj(output_path)
        assert validation.textured_face_count == 1
        assert validation.uv_coverage == 0.5
        assert np.array_equal(
            parsed.triangle_texcoord_indices_int32,
            snapshot.triangle_texcoord_indices_int32,
        )
        face_lines = tuple(
            line for line in output_path.read_text(encoding="utf-8").splitlines() if line.startswith("f ")
        )
        assert "/" in face_lines[0]
        assert "/" not in face_lines[1]


def test_obj_export_handles_600k_face_boundary() -> None:
    """대형 600,000면 출력을 전체 문자열 조립 없이 chunk 작업으로 완료하는지 확인합니다."""

    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        snapshot = _snapshot(
            root,
            "boundary-600k",
            np.eye(4, dtype=np.float64),
            face_count=600_000,
        )
        output_root = root / "aligned_obj"
        job = ObjExportJob(
            output_root,
            (snapshot,),
            overwrite_existing_output=False,
            job_id="boundary-600k",
        )
        job.step(0.0)
        job.step(0.0)
        job.step(0.0)
        assert 0 < job.written_vertex_count <= 4096
        assert job.written_face_count == 0
        _finish_job(job, max_steps=2_000, time_budget_seconds=0.025)
        result = job.finalize()
        assert result.vertex_count == 4
        assert result.face_count == 600_000
        assert (output_root / snapshot.output_filename).is_file()


def test_obj_export_cancel_and_overwrite_preserve_atomicity() -> None:
    """취소는 기존 출력을 유지하고 승인된 성공만 aligned_obj를 교체하는지 확인합니다."""

    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        output_root = root / "aligned_obj"
        output_root.mkdir()
        sentinel = output_root / "old.txt"
        sentinel.write_text("keep", encoding="utf-8")
        snapshot = _snapshot(root, "cancel", np.eye(4, dtype=np.float64))
        cancelled = ObjExportJob(
            output_root,
            (snapshot,),
            overwrite_existing_output=True,
            job_id="cancel",
        )
        cancelled.step(0.0)
        cancelled.request_cancel()
        cancelled.step(0.0)
        assert cancelled.status == "CANCELLED"
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert cancelled.candidate_root.exists() is False

        replacement = ObjExportJob(
            output_root,
            (snapshot,),
            overwrite_existing_output=True,
            job_id="overwrite",
        )
        _finish_job(replacement)
        replacement.finalize()
        assert sentinel.exists() is False
        assert (output_root / snapshot.output_filename).is_file()
        assert replacement.backup_root.exists() is False


def test_obj_export_backup_cleanup_failure_keeps_successful_promoted_output() -> None:
    """후보 승격 뒤 백업 삭제 실패는 완료 결과와 새 출력을 유지하고 정리 경고를 남깁니다."""

    from tools.align.operators import export_obj

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        output_root = root / "aligned_obj"
        output_root.mkdir()
        (output_root / "old.txt").write_text("keep until promotion", encoding="utf-8")
        snapshot = _snapshot(root, "cleanup-warning", np.eye(4, dtype=np.float64))
        job = export_obj.ObjExportJob(
            output_root,
            (snapshot,),
            overwrite_existing_output=True,
            job_id="cleanup-warning",
        )
        _finish_job(job)
        original_remove = export_obj._remove_job_directory

        def fail_backup_cleanup(path, parent, prefix, job_id):
            """이전 출력 백업 정리에만 실패를 주입하고 다른 전용 정리는 유지합니다."""

            if prefix == export_obj.OBJ_EXPORT_PREVIOUS_OUTPUT_PREFIX:
                raise OSError("테스트 백업 삭제 실패")
            return original_remove(path, parent, prefix, job_id)

        with patch.object(export_obj, "_remove_job_directory", new=fail_backup_cleanup):
            result = job.finalize()
        assert job.status == "COMPLETED"
        assert result.output_root == output_root
        assert (output_root / snapshot.output_filename).is_file()
        assert not (output_root / "old.txt").exists()
        assert job.backup_root.is_dir()
        assert "이전 출력 백업을 정리하지 못했습니다" in result.cleanup_warning
        original_remove(
            job.backup_root,
            job.parent,
            export_obj.OBJ_EXPORT_PREVIOUS_OUTPUT_PREFIX,
            job.job_id,
        )


def test_obj_export_snapshot_fingerprint_detects_same_count_geometry_changes() -> None:
    """같은 count의 정점·UV 값과 내장 식별자 변경도 stale 지문으로 검출합니다."""

    from tools.align.operators.export_obj import (
        build_obj_export_snapshots,
        obj_export_snapshot_fingerprint,
    )

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        original = _snapshot(root, "same-count-stale", np.eye(4), with_uv=True)
        changed_vertices = original.vertices_float64.copy()
        changed_vertices[0, 0] = 0.125
        changed_uv = original.texcoords_float64.copy()
        changed_uv[0, 0] = 0.375
        vertex_mutation = replace(original, vertices_float64=changed_vertices)
        uv_mutation = replace(original, texcoords_float64=changed_uv)
        original_fingerprint = obj_export_snapshot_fingerprint((original,))
        assert obj_export_snapshot_fingerprint((vertex_mutation,)) != original_fingerprint
        assert obj_export_snapshot_fingerprint((uv_mutation,)) != original_fingerprint
        runtime, mesh, materials, _source_path, _asset_uuid = _embedded_polygon_runtime(
            root,
            "fingerprint-embedded",
        )
        try:
            embedded = build_obj_export_snapshots(runtime)[0]
            embedded_fingerprint = obj_export_snapshot_fingerprint((embedded,))
            identity_mutations = (
                replace(embedded, embedded_asset_uuid="f" * 32),
                replace(
                    embedded,
                    embedded_asset_schema_version=embedded.embedded_asset_schema_version + 1,
                ),
                replace(embedded, embedded_data_name_hint=f"{embedded.embedded_data_name_hint}-other"),
                replace(embedded, geometry_revision=embedded.geometry_revision + 1),
            )
            assert all(
                obj_export_snapshot_fingerprint((mutation,)) != embedded_fingerprint
                for mutation in identity_mutations
            )
        finally:
            _remove_embedded_fixture(mesh, materials)


def test_obj_export_cancel_during_texture_copy_preserves_previous_output() -> None:
    """대형 원본 이미지 복사 중 취소해도 기존 aligned_obj와 원본을 유지하는지 확인합니다."""

    from tools.align.constants import OBJ_EXPORT_STAGE_COPYING_TEXTURES
    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        initial = _snapshot(root, "large-texture", np.eye(4), with_texture=True)
        initial.texture_assets[0].source_path.write_bytes(b"texture-source" * 300_000)
        snapshot = replace(initial, materials=initial.materials)
        output_root = root / "aligned_obj"
        output_root.mkdir()
        sentinel = output_root / "old.txt"
        sentinel.write_text("keep", encoding="utf-8")
        job = ObjExportJob(
            output_root,
            (snapshot,),
            overwrite_existing_output=True,
            job_id="cancel-texture",
        )
        for _index in range(100):
            job.step(0.0)
            if job.stage == OBJ_EXPORT_STAGE_COPYING_TEXTURES:
                break
        else:
            raise AssertionError("텍스처 복사 단계에 도달하지 못했습니다.")
        job.step(0.0)
        assert job.texture_source_stream is not None
        job.request_cancel()
        job.step(0.0)
        assert job.status == "CANCELLED"
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert snapshot.texture_assets[0].source_path.is_file()
        assert job.candidate_root.exists() is False


def test_obj_export_reports_windows_path_limit_before_file_copy() -> None:
    """텍스처 출력 경로가 260자를 넘으면 Errno 대신 길이와 해결 방법을 표시하는지 확인합니다."""

    from tools.align.mesh_io import ObjMaterialData
    from tools.align.operators.export_obj import ObjExportJob

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        base = _snapshot(root, "path-limit", np.eye(4), with_uv=True)
        texture_path = root / (("긴텍스처이름" * 30) + ".png")
        texture_path.write_bytes(b"texture")
        snapshot = replace(
            base,
            materials=(
                ObjMaterialData(
                    name="PathLimitMaterial",
                    diffuse_texture_path=texture_path,
                ),
            ),
            triangle_material_indices_int32=np.zeros((base.face_count,), dtype=np.int32),
        )
        job = ObjExportJob(
            root / "aligned_obj",
            (snapshot,),
            overwrite_existing_output=False,
            job_id="path-limit",
        )
        job.step(0.0)
        assert job.status == "FAILED"
        assert "Windows 경로 길이 제한 260자를 초과" in job.error_message
        assert "현재" in job.error_message
        assert "더 짧은 출력 폴더" in job.error_message
        assert "No such file or directory" not in job.error_message
        assert job.candidate_root.exists() is False


def test_obj_export_snapshot_uses_only_registered_meshes() -> None:
    """등록 메시만 포함하고 전역 또는 source metadata가 있는 OBJ는 내장 UUID를 요구합니다."""

    from tools.align.alignment.types import Sim3Transform
    from tools.align.display_axes import identity_display_transform
    from tools.align.mesh_io import TriangleMeshModelData
    from tools.align.operators import export_obj
    from tools.align.operators.export_obj import build_obj_export_snapshots
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        runtime = RuntimeState()
        vertices = np.asarray(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            dtype=np.float64,
        )
        triangles = np.asarray(((0, 1, 2),), dtype=np.uint32)
        texcoords = np.asarray(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), dtype=np.float64)
        component_counts = np.asarray((2, 2, 2), dtype=np.uint8)
        face_texcoords = np.asarray(((0, 1, 2),), dtype=np.int32)
        for index, model_id in enumerate(("registered", "pending"), start=1):
            path = Path(os.path.normcase(os.path.normpath(str(root / f"{model_id}.obj"))))
            path.write_text("# source\n", encoding="utf-8")
            model = TriangleMeshModelData(
                model_id=model_id,
                display_name=model_id,
                source_path=path,
                vertices_float64=vertices,
                triangles_uint32=triangles,
                registration_vertex_indices=np.asarray((0, 1, 2), dtype=np.uint32),
                display_vertex_indices=np.asarray((0, 1, 2), dtype=np.uint32),
                bounds_min=np.asarray((0.0, 0.0, 0.0), dtype=np.float64),
                bounds_max=np.asarray((1.0, 1.0, 0.0), dtype=np.float64),
                texcoords_float64=texcoords,
                texcoord_component_counts_uint8=component_counts,
                triangle_texcoord_indices_int32=face_texcoords,
            )
            runtime.model_repository.add_model(model, identity_display_transform())
            runtime.model_catalog.add_imported_model(
                model_id,
                model_id,
                select_after_import=False,
                geometry_kind="TRIANGLE_MESH",
            )
        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, :3] *= 1.5
        matrix[:3, 3] = (2.0, 3.0, 4.0)
        runtime.model_repository.set_alignment_transform(
            "registered",
            Sim3Transform(matrix_float64=matrix),
        )
        original_vertices = runtime.model_repository.models["registered"].vertices_float64.copy()
        original_texcoords = runtime.model_repository.models["registered"].texcoords_float64.copy()
        snapshots = build_obj_export_snapshots(runtime)
        assert tuple(item.model_id for item in snapshots) == ("registered",)
        assert np.array_equal(snapshots[0].project_world_matrix_float64, matrix)
        assert np.array_equal(
            runtime.model_repository.models["registered"].vertices_float64,
            original_vertices,
        )
        assert np.array_equal(snapshots[0].texcoords_float64, original_texcoords)
        assert np.array_equal(
            runtime.model_repository.models["registered"].texcoords_float64,
            original_texcoords,
        )
        with (
            patch.object(export_obj.RUNTIME, "model_repository", runtime.model_repository),
            patch.object(export_obj.RUNTIME, "model_catalog", runtime.model_catalog),
        ):
            try:
                export_obj.build_obj_export_snapshots()
            except RuntimeError as error:
                assert "내장 Mesh 식별자가 필요" in str(error)
            else:
                raise AssertionError("전역 Runtime OBJ의 내장 Mesh 누락을 허용했습니다.")
        runtime.model_repository.source_metadata["registered"] = object()
        try:
            build_obj_export_snapshots(runtime)
        except RuntimeError as error:
            assert "내장 Mesh 식별자가 필요" in str(error)
        else:
            raise AssertionError("신규 OBJ 모델의 내장 Mesh 누락을 허용했습니다.")


def test_exported_obj_imports_uv_face_corners_into_blender() -> None:
    """내보낸 OBJ를 Blender로 다시 가져와 face-corner UV와 seam이 유지되는지 확인합니다."""

    import bpy

    from tools.align.mesh_io.obj_writer import write_obj_incremental

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        snapshot = _snapshot(
            root,
            "blender-uv-consumer",
            np.eye(4, dtype=np.float64),
            with_uv=True,
        )
        output_path = root / snapshot.output_filename
        write_obj_incremental(output_path, snapshot)
        imported_before = set(bpy.data.objects)
        result = bpy.ops.wm.obj_import(filepath=str(output_path))
        assert "FINISHED" in result
        imported = tuple(obj for obj in bpy.data.objects if obj not in imported_before)
        assert len(imported) == 1
        mesh_object = imported[0]
        mesh = mesh_object.data
        assert mesh_object.type == "MESH"
        assert mesh.uv_layers.active is not None
        assert len(mesh.polygons) == 2
        expected_indices = snapshot.triangle_texcoord_indices_int32
        for polygon_index, polygon in enumerate(mesh.polygons):
            for corner_index, loop_index in enumerate(polygon.loop_indices):
                uv_index = int(expected_indices[polygon_index, corner_index])
                expected_uv = snapshot.texcoords_float64[uv_index, :2]
                actual_uv = np.asarray(
                    mesh.uv_layers.active.uv[loop_index].vector,
                    dtype=np.float64,
                )
                assert np.allclose(actual_uv, expected_uv, rtol=0.0, atol=1.0e-6)
        first_face_uv = np.asarray(mesh.uv_layers.active.uv[mesh.polygons[0].loop_start].vector)
        second_face_uv = np.asarray(mesh.uv_layers.active.uv[mesh.polygons[1].loop_start].vector)
        assert not np.allclose(first_face_uv, second_face_uv)
        bpy.data.objects.remove(mesh_object, do_unlink=True)


def test_exported_obj_imports_copied_texture_into_blender() -> None:
    """내보낸 OBJ를 Blender가 다시 가져올 때 복사 텍스처가 재질에 연결되는지 확인합니다."""

    import bpy

    from tools.align.mesh_io.obj_writer import write_obj_incremental

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        snapshot = _snapshot(
            root,
            "blender-texture-consumer",
            np.eye(4, dtype=np.float64),
            with_texture=True,
        )
        output_path = root / snapshot.output_filename
        write_obj_incremental(output_path, snapshot)
        copied_texture = (root / snapshot.texture_assets[0].output_relative_path).resolve()
        imported_before = set(bpy.data.objects)
        result = bpy.ops.wm.obj_import(filepath=str(output_path))
        assert "FINISHED" in result
        imported = tuple(obj for obj in bpy.data.objects if obj not in imported_before)
        assert len(imported) == 1
        mesh_object = imported[0]
        assert len(mesh_object.data.materials) == 1
        material = mesh_object.data.materials[0]
        image_nodes = tuple(
            node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeTexImage"
        )
        assert len(image_nodes) == 1
        loaded_path = Path(bpy.path.abspath(image_nodes[0].image.filepath)).resolve()
        assert loaded_path == copied_texture
        assert copied_texture.read_bytes() == snapshot.texture_assets[0].source_path.read_bytes()
        bpy.data.objects.remove(mesh_object, do_unlink=True)


def test_embedded_quad_ngon_export_survives_deleted_source_without_external_geometry_io() -> None:
    """삭제된 외부 OBJ를 읽지 않고 내장 Quad·N-gon·UV seam·재질을 원형 Export합니다."""

    import bpy

    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io import obj_writer
    from tools.align.operators.export_obj import ObjExportJob, build_obj_export_snapshots

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        runtime, mesh, materials, source_path, _asset_uuid = _embedded_polygon_runtime(root)
        original_vertices = tuple(tuple(float(value) for value in vertex.co) for vertex in mesh.vertices)
        original_polygons = tuple(tuple(polygon.vertices) for polygon in mesh.polygons)
        source_path.unlink()
        path_type = type(source_path)
        original_open = path_type.open
        original_stat = path_type.stat
        original_sha256 = obj_writer.sha256_file

        def guarded_open(path, *args, **kwargs):
            """외부 OBJ open을 거부하고 다른 출력·텍스처 파일 접근은 통과시킵니다."""

            if path == source_path:
                raise AssertionError("내장 OBJ Export가 외부 OBJ를 열었습니다.")
            return original_open(path, *args, **kwargs)

        def guarded_stat(path, *args, **kwargs):
            """외부 OBJ stat을 거부하고 다른 파일 상태 확인은 통과시킵니다."""

            if path == source_path:
                raise AssertionError("내장 OBJ Export가 외부 OBJ 상태를 조회했습니다.")
            return original_stat(path, *args, **kwargs)

        def guarded_sha256(path):
            """외부 OBJ SHA 계산을 거부하고 후보 출력 해시는 기존 함수로 계산합니다."""

            if Path(path) == source_path:
                raise AssertionError("내장 OBJ Export가 외부 OBJ SHA를 계산했습니다.")
            return original_sha256(path)

        try:
            with (
                patch.object(path_type, "open", new=guarded_open),
                patch.object(path_type, "stat", new=guarded_stat),
                patch.object(obj_writer, "sha256_file", new=guarded_sha256),
            ):
                snapshot = build_obj_export_snapshots(runtime)[0]
                assert snapshot.face_count == 2
                assert np.array_equal(snapshot.polygon_loop_totals_uint32, (4, 5))
                assert tuple(material.name for material in snapshot.materials) == (
                    "원본 재질 A",
                    "원본 재질 B",
                )
                job = ObjExportJob(
                    root / "aligned_obj",
                    (snapshot,),
                    overwrite_existing_output=False,
                    job_id="embedded-deleted-source",
                )
                _finish_job(job)
                result = job.finalize()
            output_path = result.output_root / snapshot.output_filename
            parsed = read_obj(output_path, preserve_polygons=True)
            assert parsed.polygon_count == 2
            assert np.array_equal(parsed.polygon_loop_totals_uint32, (4, 5))
            assert np.array_equal(
                parsed.loop_vertex_indices_uint32,
                snapshot.loop_vertex_indices_uint32,
            )
            assert np.array_equal(
                parsed.loop_texcoord_indices_int32,
                snapshot.loop_texcoord_indices_int32,
            )
            assert np.array_equal(parsed.polygon_material_indices_int32, (0, 1))
            assert not np.allclose(parsed.texcoords_float64[0], parsed.texcoords_float64[4])
            imported_before = set(bpy.data.objects)
            assert "FINISHED" in bpy.ops.wm.obj_import(filepath=str(output_path))
            imported = tuple(obj for obj in bpy.data.objects if obj not in imported_before)
            assert len(imported) == 1
            assert tuple(polygon.loop_total for polygon in imported[0].data.polygons) == (4, 5)
            bpy.data.objects.remove(imported[0], do_unlink=True)
            assert tuple(tuple(float(value) for value in vertex.co) for vertex in mesh.vertices) == original_vertices
            assert tuple(tuple(polygon.vertices) for polygon in mesh.polygons) == original_polygons
        finally:
            _remove_embedded_fixture(mesh, materials)


def test_embedded_polygon_reflection_reverses_vertex_and_uv_corners_together() -> None:
    """Reflection에서 각 Polygon 첫 Corner를 고정하고 나머지 정점·UV 순서를 함께 뒤집습니다."""

    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io.obj_writer import write_obj_incremental
    from tools.align.operators.export_obj import build_obj_export_snapshots

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        runtime, mesh, materials, _source_path, _asset_uuid = _embedded_polygon_runtime(
            root,
            "embedded-reflection",
        )
        try:
            snapshot = build_obj_export_snapshots(runtime)[0]
            reflection = np.eye(4, dtype=np.float64)
            reflection[0, 0] = -2.0
            reflection[1, 1] = 2.0
            reflection[2, 2] = 2.0
            reflection[:3, 3] = (10.0, 20.0, 30.0)
            reflected = replace(snapshot, project_world_matrix_float64=reflection)
            output_path = root / reflected.output_filename
            write_obj_incremental(output_path, reflected, face_chunk_size=1)
            parsed = read_obj(output_path, preserve_polygons=True)
            assert np.allclose(parsed.vertices_float64[1], (6.0, 20.0, 30.0))
            assert np.array_equal(parsed.polygon_loop_totals_uint32, (4, 5))
            assert np.array_equal(
                parsed.loop_vertex_indices_uint32,
                np.asarray((0, 3, 2, 1, 0, 6, 5, 4, 3), dtype=np.uint32),
            )
            assert np.array_equal(
                parsed.loop_texcoord_indices_int32,
                np.asarray((0, 3, 2, 1, 4, 8, 7, 6, 5), dtype=np.int32),
            )
            assert np.array_equal(parsed.polygon_material_indices_int32, (0, 1))
        finally:
            _remove_embedded_fixture(mesh, materials)


def test_embedded_revision_mismatch_discards_candidate_and_preserves_previous_output() -> None:
    """내장 revision 불일치는 후보만 폐기하고 기존 aligned_obj를 유지합니다."""

    from tools.align.operators.export_obj import ObjExportJob, build_obj_export_snapshots

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        runtime, mesh, materials, _source_path, _asset_uuid = _embedded_polygon_runtime(
            root,
            "embedded-revision-mismatch",
        )
        output_root = root / "aligned_obj"
        output_root.mkdir()
        sentinel = output_root / "old.txt"
        sentinel.write_text("keep", encoding="utf-8")
        try:
            snapshot = build_obj_export_snapshots(runtime)[0]
            job = ObjExportJob(
                output_root,
                (snapshot,),
                overwrite_existing_output=True,
                job_id="embedded-revision-mismatch",
            )
            _finish_job(job)
            assert job.is_ready_to_finalize
            mesh["geometry_revision"] = snapshot.geometry_revision + 1
            try:
                build_obj_export_snapshots(runtime)
            except Exception as error:
                job.mark_stale(str(error))
            else:
                raise AssertionError("내장 OBJ revision 불일치를 허용했습니다.")
            assert job.status == "STALE"
            assert not job.candidate_root.exists()
            assert sentinel.read_text(encoding="utf-8") == "keep"
        finally:
            _remove_embedded_fixture(mesh, materials)


def run_all() -> None:
    """OBJ Export 회귀 테스트를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_obj_export_filename_uses_only_model_display_name()
    test_obj_writer_bakes_transform_and_reflection_winding()
    test_obj_export_job_is_incremental_atomic_and_reported()
    test_obj_export_copies_original_texture_and_generates_mtl()
    test_obj_export_shares_same_flat_texture_across_models()
    test_obj_export_rejects_different_flat_textures_with_same_name()
    test_obj_export_rejects_duplicate_model_display_names()
    test_obj_export_preserves_partial_uv_face_set()
    test_obj_export_handles_600k_face_boundary()
    test_obj_export_cancel_and_overwrite_preserve_atomicity()
    test_obj_export_backup_cleanup_failure_keeps_successful_promoted_output()
    test_obj_export_snapshot_fingerprint_detects_same_count_geometry_changes()
    test_obj_export_cancel_during_texture_copy_preserves_previous_output()
    test_obj_export_reports_windows_path_limit_before_file_copy()
    test_obj_export_snapshot_uses_only_registered_meshes()
    test_exported_obj_imports_uv_face_corners_into_blender()
    test_exported_obj_imports_copied_texture_into_blender()
    test_embedded_quad_ngon_export_survives_deleted_source_without_external_geometry_io()
    test_embedded_polygon_reflection_reverses_vertex_and_uv_corners_together()
    test_embedded_revision_mismatch_discards_candidate_and_preserves_previous_output()
    print("test_obj_export: 통과")


if __name__ == "__main__":
    run_all()
