"""연결 OBJ의 형상 전용 읽기·준비 원자성·안전한 후보 선택을 검증합니다."""

from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import numpy as np


_GEOMETRY = "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
_CHECK = TestCase()


def _add_source_path() -> None:
    """실험 작업트리의 소스 경로를 Blender 모듈 검색 경로에 추가합니다."""
    source = Path(__file__).resolve().parents[3]
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


def _write_obj(path: Path, body: str) -> Path:
    """테스트 전용 OBJ를 UTF-8로 기록하고 경로를 반환합니다."""
    path.write_text(body, encoding="utf-8")
    return path


def _read_incrementally(path: Path, **kwargs):
    """최소 시간 예산으로 OBJ를 끝까지 읽어 증분 결과를 반환합니다."""
    from tools.align.mesh_io import ObjReader

    reader = ObjReader(path, **kwargs)
    for _index in range(10_000):
        reader.step(time_budget_seconds=0.0)
        if reader.done:
            return reader.model
    raise AssertionError("OBJ 증분 읽기가 제한 단계 안에 끝나지 않았습니다.")


def _run_job(job, runtime=None):
    """가져오기 작업을 최소 시간 예산으로 진행하고 방문 단계를 반환합니다."""
    stages = []
    for _index in range(10_000):
        stages.append(job.stage)
        result = job.step(runtime, time_budget_seconds=0.0)
        if job.is_terminal:
            return stages, result
    raise AssertionError("OBJ 준비 작업이 제한 단계 안에 끝나지 않았습니다.")


def _advance_to(job, stage: str) -> None:
    """취소·파일 변경을 주입할 실제 작업 단계까지 진행합니다."""
    for _index in range(10_000):
        if job.stage == stage:
            return
        assert not job.is_terminal, job.error_message
        job.step(time_budget_seconds=0.0)
    raise AssertionError(f"목표 단계에 도달하지 못했습니다: {stage}")


def _assert_geometry_only(result, face_count: int) -> None:
    """형상 전용 결과가 기존 빈 UV·재질 및 읽기 전용 배열 계약을 지키는지 확인합니다."""
    assert result.vertex_count == 4
    assert result.triangle_count == face_count
    assert result.texcoords_float64.shape == (0, 3)
    assert result.texcoord_component_counts_uint8.shape == (0,)
    assert result.triangle_texcoord_indices_int32.shape == (face_count, 3)
    assert np.all(result.triangle_texcoord_indices_int32 == -1)
    assert result.materials == ()
    assert result.triangle_material_indices_int32.shape == (face_count,)
    assert np.all(result.triangle_material_indices_int32 == -1)
    for array in (
        result.vertices_float64,
        result.triangles_uint32,
        result.texcoords_float64,
        result.texcoord_component_counts_uint8,
        result.triangle_texcoord_indices_int32,
        result.triangle_material_indices_int32,
    ):
        assert array.flags.writeable is False


def test_geometry_only_ignores_materials_and_invalid_uv_without_external_reads() -> None:
    """동기·증분 읽기 모두 외부 재질 접근 없이 잘못된 UV·법선·재질 구문을 무시합니다."""
    from tools.align.mesh_io import obj_reader

    with TemporaryDirectory() as temporary:
        path = _write_obj(
            Path(temporary) / "geometry.obj",
            "mtllib missing.mtl\nmtllib \"broken\nusemtl\nvt nan invalid 0 1\nvn invalid\n"
            + _GEOMETRY
            + "f 1 2 3\nf 1/999 2/0 4/bad\nf 1//bad 3//0 4//999\n"
            + "f 1/no/normal 2/999/0 3/-99/none\nf -4/bad -3//bad -2/0/0\n",
        )
        opened = []
        original_open = Path.open

        def audited_open(candidate, *args, **kwargs):
            """원본 OBJ 이외의 파일 열기를 즉시 실패시킵니다."""
            opened.append(candidate)
            assert candidate == path, f"외부 파일을 열었습니다: {candidate}"
            return original_open(candidate, *args, **kwargs)

        with (
            patch.object(Path, "open", audited_open),
            patch.object(obj_reader, "resolve_obj_materials", side_effect=AssertionError("재질 해석 호출")) as resolver,
            patch.object(obj_reader, "split_obj_arguments", side_effect=AssertionError("재질 구문 해석 호출")),
        ):
            direct = obj_reader.read_obj(path, geometry_only=True)
            incremental = _read_incrementally(path, geometry_only=True)
        assert opened == [path, path]
        resolver.assert_not_called()
        _assert_geometry_only(direct, 5)
        _assert_geometry_only(incremental, 5)
        assert np.array_equal(direct.triangles_uint32[-1], (0, 1, 2))
        assert np.array_equal(direct.triangles_uint32, incremental.triangles_uint32)


def test_geometry_only_preserves_validation_without_default_face_limit() -> None:
    """잘못된 형상은 거부하고 기존 600,000면 경계를 넘는 입력은 전체 읽습니다."""
    from tools.align.mesh_io import ObjReaderError, read_obj

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        for index, body in enumerate((
            _GEOMETRY + "f 0/ignored 2 3\n",
            _GEOMETRY + "f 1 2 5/ignored\n",
            _GEOMETRY + "f -5 2 3\n",
            _GEOMETRY + "f 1 1 3\n",
            _GEOMETRY + "f 1 2 3 4\n",
            "v nan 0 0\n" + _GEOMETRY + "f 2 3 4\n",
            "v inf 0 0\n" + _GEOMETRY + "f 2 3 4\n",
        )):
            path = _write_obj(root / f"invalid_{index}.obj", body)
            for reader in (read_obj, _read_incrementally):
                with _CHECK.assertRaises(ObjReaderError):
                    reader(path, geometry_only=True)
        face_count = 600_001
        path = _write_obj(
            root / "over_previous_limit.obj",
            _GEOMETRY + "f 1/invalid 2/none 3/0\n" * face_count,
        )
        result = read_obj(path, geometry_only=True)
        assert result.triangle_count == face_count
        del result
        limited_path = _write_obj(root / "caller_limit.obj", _GEOMETRY + "f 1 2 3\n" * 3)
        with _CHECK.assertRaisesRegex(ObjReaderError, "2개 호출 제한"):
            read_obj(limited_path, geometry_only=True, max_faces=2)


def test_default_mode_preserves_uv_materials_and_rejects_bad_uv() -> None:
    """기본 전체 읽기는 UV·재질을 계속 보존하고 잘못된 UV 참조를 거부합니다."""
    from tools.align.mesh_io import ObjReaderError, read_obj

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "mesh.mtl").write_text("newmtl Surface\nKd 0.2 0.3 0.4\n", encoding="utf-8")
        path = _write_obj(root / "mesh.obj", "mtllib mesh.mtl\nusemtl Surface\n" + _GEOMETRY + "vt 0 0\nf 1/1 2/1 3/1\n")
        for reader in (read_obj, _read_incrementally):
            result = reader(path)
            assert result.texture_coordinate_count == 1
            assert result.materials[0].name == "Surface"
            assert result.materials[0].diffuse_color == (0.2, 0.3, 0.4)
            assert np.all(result.triangle_material_indices_int32 == 0)
        _write_obj(path, _GEOMETRY + "f 1/missing 2/0 3/99\n")
        with _CHECK.assertRaises(ObjReaderError):
            read_obj(path)


def test_stage_only_prepares_without_runtime_gpu_or_registration() -> None:
    """준비 전용 Native 작업은 런타임·GPU 없이 축·구버전 지문·프록시를 준비합니다."""
    import tools.align.mesh_importer as importer
    from tools.align.mesh_io import obj_reader

    class UntouchableRuntime:
        """준비 단계에서 런타임에 접근하면 실패시키는 감시 객체입니다."""

        def __getattr__(self, name):
            """저장소·카탈로그를 포함한 모든 런타임 참조를 거부합니다."""
            raise AssertionError(f"준비 단계에서 런타임에 접근했습니다: {name}")

    with TemporaryDirectory() as temporary:
        path = _write_obj(Path(temporary) / "staged.obj", "mtllib missing.mtl\n" + _GEOMETRY + "f 1/bad 2/0 3/99\n" * 1_000)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with (
            patch.object(importer, "_prepare_gpu_batch_for_model", side_effect=AssertionError("GPU 준비 호출")) as prepare,
            patch.object(importer, "_discard_gpu_batch_for_model", side_effect=AssertionError("GPU 삭제 호출")) as discard,
            patch.object(obj_reader, "resolve_obj_materials", side_effect=AssertionError("재질 해석 호출")),
        ):
            for runtime in (None, UntouchableRuntime()):
                job = importer.MeshImportJob(
                    str(path), geometry_only=True, stage_only=True,
                    expected_source_fingerprint=expected,
                    axis_preset="CUSTOM", axis_forward="-Z", axis_up="Y",
                    display_vertex_limit=2,
                )
                stages, result = _run_job(job, runtime)
                assert result == "FINISHED", job.error_message
                assert job.stage == importer.MESH_IMPORT_STAGE_COMPLETED
                assert job.progress == 1.0
                assert job.source_fingerprint_before == expected
                assert job.axis_forward == "-Z" and job.axis_up == "Y"
                assert importer.MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE in stages
                assert importer.MESH_IMPORT_STAGE_HASH_SOURCE_AFTER in stages
                assert importer.MESH_IMPORT_STAGE_PREPARE_GPU not in stages
                assert importer.MESH_IMPORT_STAGE_COMMIT not in stages
                assert stages.count(importer.MESH_IMPORT_STAGE_READ_OBJ) == 1
                assert job.staged_model.original_polygon_count == 1
                assert job.staged_model.face_count == 1
                assert job.staged_model.display_vertex_indices.size == 2
                assert job.staged_model.texture_coordinate_count == 0
                assert job.staged_model.materials == ()
                assert job.reader is None and job.parsed_result is None
                assert job.fingerprint_job is None and job.display_builder is None
                assert job.gpu_batch_prepared is False
                assert job.step(runtime) == "FINISHED"
            prepare.assert_not_called()
            discard.assert_not_called()


def test_gpu_disabled_import_registers_and_rolls_back_without_gpu_access() -> None:
    """GPU 비활성 가져오기는 등록을 수행하고 등록 실패도 GPU 접근 없이 되돌립니다."""
    import tools.align.mesh_importer as importer
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary:
        path = _write_obj(Path(temporary) / "restored.obj", _GEOMETRY + "f 1 2 3\n")
        with (
            patch.object(importer, "_prepare_gpu_batch_for_model", side_effect=AssertionError("GPU 준비 호출")) as prepare,
            patch.object(importer, "_discard_gpu_batch_for_model", side_effect=AssertionError("GPU 삭제 호출")) as discard,
        ):
            runtime = RuntimeState()
            job = importer.MeshImportJob(str(path), prepare_gpu=False)
            stages, result = _run_job(job, runtime)
            assert result == "FINISHED", job.error_message
            assert importer.MESH_IMPORT_STAGE_PREPARE_GPU not in stages
            assert importer.MESH_IMPORT_STAGE_COMMIT in stages
            assert job.staged_model.model_id in runtime.model_repository.models
            assert job.staged_model.model_id in runtime.model_catalog.models
            failed_runtime = RuntimeState()
            failed = importer.MeshImportJob(str(path), prepare_gpu=False)
            with patch.object(type(failed_runtime.model_catalog), "add_imported_model", side_effect=RuntimeError("등록 실패")):
                _run_job(failed, failed_runtime)
            assert failed.stage == importer.MESH_IMPORT_STAGE_FAILED
            assert not failed_runtime.model_repository.models
            assert not failed_runtime.model_catalog.models
            prepare.assert_not_called()
            discard.assert_not_called()


def test_stage_only_cancellation_cleans_only_temporary_resources() -> None:
    """준비 각 경계의 취소가 원본을 보존하고 임시 배열·파일 핸들만 해제합니다."""
    import tools.align.mesh_importer as importer

    stages = (
        importer.MESH_IMPORT_STAGE_FILE_CHECK,
        importer.MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE,
        importer.MESH_IMPORT_STAGE_READ_OBJ,
        importer.MESH_IMPORT_STAGE_HASH_SOURCE_AFTER,
        importer.MESH_IMPORT_STAGE_PREPARE_MODEL,
        importer.MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE,
    )
    with TemporaryDirectory() as temporary:
        path = _write_obj(Path(temporary) / "cancel.obj", _GEOMETRY + "f 1 2 3\n" * 1_000)
        original = path.read_bytes()
        with patch.object(importer, "_discard_gpu_batch_for_model", side_effect=AssertionError("GPU 삭제 호출")):
            for stage in stages:
                job = importer.MeshImportJob(str(path), geometry_only=True, stage_only=True)
                _advance_to(job, stage)
                if stage in {importer.MESH_IMPORT_STAGE_READ_OBJ, importer.MESH_IMPORT_STAGE_HASH_SOURCE_BEFORE}:
                    job.step(time_budget_seconds=0.0)
                reader = job.reader
                fingerprint_job = job.fingerprint_job
                assert job.request_cancel()
                assert job.step() == "CANCELLED"
                assert job.stage == importer.MESH_IMPORT_STAGE_CANCELLED
                assert job.staged_model is None and job.parsed_result is None
                assert job.reader is None and job.fingerprint_job is None
                assert job.display_builder is None and job.display_indices is None
                if reader is not None:
                    assert reader._stream is None
                if fingerprint_job is not None:
                    assert fingerprint_job._stream is None
                assert path.read_bytes() == original


def test_stage_only_rejects_expected_or_changed_source_fingerprints() -> None:
    """예상 지문 불일치와 읽기 도중 원본 변경은 모델을 남기지 않고 실패합니다."""
    import tools.align.mesh_importer as importer

    with TemporaryDirectory() as temporary:
        path = _write_obj(Path(temporary) / "changed.obj", _GEOMETRY + "f 1 2 3\n")
        mismatch = importer.MeshImportJob(str(path), geometry_only=True, stage_only=True, expected_source_fingerprint="0" * 64)
        _run_job(mismatch)
        assert mismatch.stage == importer.MESH_IMPORT_STAGE_FAILED
        assert mismatch.staged_model is None
        assert "fingerprint" in mismatch.error_message
        changed = importer.MeshImportJob(str(path), geometry_only=True, stage_only=True)
        _advance_to(changed, importer.MESH_IMPORT_STAGE_HASH_SOURCE_AFTER)
        path.write_text(_GEOMETRY + "f 1 3 2\n", encoding="utf-8")
        _run_job(changed)
        assert changed.stage == importer.MESH_IMPORT_STAGE_FAILED
        assert changed.staged_model is None and changed.reader is None
        assert "변경" in changed.error_message
        with _CHECK.assertRaises(importer.MeshImportJobError):
            importer.MeshImportJob(str(path), stage_only=True, expected_source_fingerprint="invalid")
        with _CHECK.assertRaises(importer.MeshImportJobError):
            importer.MeshImportJob(str(path), stage_only=True, axis_preset="CUSTOM", axis_forward="X", axis_up="X")


def test_companion_candidates_are_direct_sorted_and_require_explicit_choice() -> None:
    """루트 직하의 0·1·복수 후보, 대소문자 확장자와 명시 선택 경계를 확인합니다."""
    from tools.align.companion_discovery import discover_companion_obj_candidates, resolve_companion_obj_path

    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "dataset"
        root.mkdir()
        (root / "nested").mkdir()
        nested = _write_obj(root / "nested" / "nested.obj", _GEOMETRY + "f 1 2 3\n")
        (root / "directory.obj").mkdir()
        (root / "ignored.mtl").write_text("invalid", encoding="utf-8")
        assert discover_companion_obj_candidates(root) == ()
        assert resolve_companion_obj_path(root) is None
        first = _write_obj(root / "B.OBJ", _GEOMETRY + "f 1 2 3\n").resolve()
        assert resolve_companion_obj_path(root) == first
        second = _write_obj(root / "a.obj", _GEOMETRY + "f 1 2 3\n").resolve()
        assert discover_companion_obj_candidates(root) == (second, first)
        with _CHECK.assertRaisesRegex(ValueError, "여러 개"):
            resolve_companion_obj_path(root)
        assert resolve_companion_obj_path(root, first) == first
        assert resolve_companion_obj_path(root, "a.obj") == second
        outside = _write_obj(Path(temporary) / "outside.obj", _GEOMETRY + "f 1 2 3\n")
        for invalid in (outside, nested, "missing.obj", "../outside.obj", "directory.obj"):
            with _CHECK.assertRaises(ValueError):
                resolve_companion_obj_path(root, invalid)


def test_companion_candidates_exclude_links_junctions_and_surface_access_errors() -> None:
    """링크·재분석 지점은 제외하고 폴더·후보 접근 오류를 명시적으로 전달합니다."""
    from tools.align.companion_discovery import discover_companion_obj_candidates

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = _write_obj(root / "candidate.obj", _GEOMETRY + "f 1 2 3\n")
        original_lstat = Path.lstat
        for status in (
            SimpleNamespace(st_mode=stat.S_IFLNK | 0o777),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x400),
        ):
            def simulated_lstat(candidate):
                """후보 파일 하나만 링크 또는 Windows 재분석 지점으로 보고합니다."""
                return status if candidate == path else original_lstat(candidate)

            with patch.object(Path, "lstat", simulated_lstat):
                assert discover_companion_obj_candidates(root) == ()
        with patch.object(Path, "iterdir", side_effect=PermissionError("폴더 접근 거부")):
            with _CHECK.assertRaisesRegex(ValueError, "탐색"):
                discover_companion_obj_candidates(root)

        def inaccessible_lstat(candidate):
            """후보 파일의 메타데이터 접근만 거부해 오류 전파를 확인합니다."""
            if candidate == path:
                raise PermissionError("파일 접근 거부")
            return original_lstat(candidate)

        with patch.object(Path, "lstat", inaccessible_lstat):
            with _CHECK.assertRaisesRegex(ValueError, "탐색"):
                discover_companion_obj_candidates(root)
        with _CHECK.assertRaises(ValueError):
            discover_companion_obj_candidates(root / "missing")


def run_all() -> None:
    """Blender에서 연결 OBJ의 읽기·준비·후보 선택 회귀를 순서대로 실행합니다."""
    _add_source_path()
    tests = (
        test_geometry_only_ignores_materials_and_invalid_uv_without_external_reads,
        test_geometry_only_preserves_validation_without_default_face_limit,
        test_default_mode_preserves_uv_materials_and_rejects_bad_uv,
        test_stage_only_prepares_without_runtime_gpu_or_registration,
        test_gpu_disabled_import_registers_and_rolls_back_without_gpu_access,
        test_stage_only_cancellation_cleans_only_temporary_resources,
        test_stage_only_rejects_expected_or_changed_source_fingerprints,
        test_companion_candidates_are_direct_sorted_and_require_explicit_choice,
        test_companion_candidates_exclude_links_junctions_and_surface_access_errors,
    )
    for test in tests:
        test()
    print(f"연결 OBJ 회귀 {len(tests)}개: 통과")


if __name__ == "__main__":
    run_all()
