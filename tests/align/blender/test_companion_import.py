"""COLMAP 소유 OBJ 연결 가져오기의 경로·원자성·원본 보존을 검증합니다."""

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import struct
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.align import importer
from tools.align.importer import create_import_job, IMPORT_STAGE_READ_COMPANION
from tools.align.colmap_io.discovery import discover_sparse_model_with_dataset_root
from tools.align.colmap_io.errors import ColmapDiscoveryError
from tools.align.state import RuntimeState


def write_dataset(root, *, mesh=True, index="0"):
    """같은 원본 좌표를 쓰는 유효 COLMAP·OBJ 데이터셋을 만듭니다."""
    sparse = root / "sparse" / index
    sparse.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    (sparse / "cameras.txt").write_text("1 PINHOLE 640 480 500 500 320 240\n", encoding="utf-8")
    (sparse / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 image.jpg\n320 240 1 420 240 2 320 340 3\n", encoding="utf-8",
    )
    (sparse / "points3D.txt").write_text(
        "1 0 0 5 255 0 0 0 1 0\n2 1 0 5 0 255 0 0 1 1\n3 0 1 5 0 0 255 0 1 2\n", encoding="utf-8",
    )
    if mesh:
        (root / "model.OBJ").write_text(
            "mtllib missing.mtl\nusemtl missing\nv 0 0 5\nv 1 0 5\nv 0 1 5\nf 1/999 2/888 3/777\n",
            encoding="utf-8",
        )
    return sparse


def finish(job, runtime):
    """증분 가져오기를 끝까지 진행하며 진행률의 단조 증가를 확인합니다."""
    previous = 0.0
    for _ in range(500):
        result = job.step(runtime)
        if result != "RUNNING":
            return result
        assert job.progress >= previous
        previous = job.progress
    raise AssertionError("연결 가져오기가 종료되지 않았습니다.")


def make_job(path, model_id="dataset", **kwargs):
    """GPU가 없는 검증용 가져오기에 고정 소유 ID를 부여합니다."""
    return create_import_job(path, model_id_factory=lambda: model_id, prepare_gpu=False, **kwargs)


def write_binary_model(directory):
    """형식 불일치 검사에 사용할 정상 binary 카메라·이미지·점 파일을 만듭니다."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.bin").write_bytes(
        struct.pack("<Q", 1) + struct.pack("<IiQQ4d", 1, 1, 640, 480, 500, 500, 320, 240)
    )
    image = struct.pack("<Q", 1) + struct.pack("<I7dI", 1, 1, 0, 0, 0, 0, 0, 0, 1)
    image += b"image.jpg\0" + struct.pack("<Q", 3)
    points = struct.pack("<Q", 3)
    for index, (x, y, z) in enumerate(((0, 0, 5), (1, 0, 5), (0, 1, 5))):
        image += struct.pack("<ddQ", 320 + x * 100, 240 + y * 100, index + 1)
        points += struct.pack("<Q3d3BdQII", index + 1, x, y, z, 255, 0, 0, 0, 1, 1, index)
    (directory / "images.bin").write_bytes(image)
    (directory / "points3D.bin").write_bytes(points)
    return directory


def assert_explicit_selection_error(path, *, input_format, expected_message):
    """명시 모델 오류를 보존하고 가져오기 실패가 아무 모델도 등록하지 않는지 확인합니다."""
    try:
        discover_sparse_model_with_dataset_root(path, input_format)
    except ColmapDiscoveryError as error:
        original_error = str(error)
        assert expected_message in original_error
        assert str(path) in original_error
    else:
        raise AssertionError("명시 모델 오류를 무시하고 다른 모델을 선택했습니다.")
    runtime = RuntimeState()
    job = make_job(path, input_format=input_format)
    assert finish(job, runtime) == "CANCELLED"
    assert original_error in job.error_message
    assert not runtime.model_repository.models
    assert not runtime.model_repository.companion_meshes
    assert not runtime.model_repository.companion_sources
    assert not runtime.model_catalog.models


def test_root_and_sparse_import_own_one_geometry_pair():
    """세 선택 위치에서 하나의 COLMAP이 원본 좌표 그대로 OBJ를 소유합니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        sparse = write_dataset(root)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}
        for selected in (root, sparse.parent, sparse):
            runtime = RuntimeState()
            job = make_job(selected)
            assert finish(job, runtime) == "FINISHED", job.error_message
            repository = runtime.model_repository
            assert tuple(repository.models) == ("dataset",)
            assert tuple(runtime.model_catalog.models) == ("dataset",)
            assert tuple(repository.companion_meshes) == ("dataset",)
            owner = repository.models["dataset"]
            mesh = repository.companion_mesh_for_model_id("dataset")
            assert mesh.model_id == owner.model_id
            assert np.array_equal(mesh.vertices_float64, owner.points.positions)
            assert mesh.texture_coordinate_count == 0 and mesh.materials == ()
            record = runtime.model_catalog.models["dataset"]
            assert not record.show_points and record.show_surface and record.is_anchor
            assert tuple(repository.alignment_transforms) == ("dataset",)
            assert tuple(repository.display_transforms) == ("dataset",)
            assert Path(job.dataset_root) == root.resolve()
            assert repository.companion_sources["dataset"].geometry_only is True
        assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}


def test_no_obj_or_disabled_keeps_colmap_only():
    """OBJ 없음과 명시적 연결 해제는 기존 COLMAP 단독 상태를 유지합니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_dataset(root, mesh=False)
        runtime = RuntimeState()
        job = make_job(root)
        assert finish(job, runtime) == "FINISHED"
        assert not runtime.model_repository.companion_meshes
        write_dataset(root)
        runtime = RuntimeState()
        job = make_job(root, import_companion_obj=False)
        assert finish(job, runtime) == "FINISHED"
        assert not runtime.model_repository.companion_meshes
        assert runtime.model_catalog.models["dataset"].show_points


def test_multiple_obj_requires_explicit_selection():
    """복수 OBJ를 임의 선택하지 않고 지정한 후보만 연결합니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_dataset(root)
        other = root / "other.obj"
        other.write_bytes((root / "model.OBJ").read_bytes())
        runtime = RuntimeState()
        job = make_job(root)
        assert finish(job, runtime) == "CANCELLED"
        assert "여러" in job.error_message
        assert not runtime.model_repository.models
        job = make_job(root, companion_obj_path=str(other))
        assert finish(job, runtime) == "FINISHED", job.error_message
        assert Path(runtime.model_repository.companion_meshes["dataset"].source_path) == other


def test_explicit_sparse_one_does_not_resolve_to_zero():
    """복수 sparse에서 직접 고른 1번 모델과 OBJ의 관계를 보존합니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_dataset(root)
        one = write_dataset(root, index="1")
        inferred_root, layout = discover_sparse_model_with_dataset_root(one)
        assert inferred_root == root and layout.source_path == one
        runtime = RuntimeState()
        job = make_job(root)
        assert finish(job, runtime) == "CANCELLED"
        assert "직접 선택" in job.error_message
        job = make_job(one)
        assert finish(job, runtime) == "FINISHED", job.error_message
        assert Path(runtime.model_repository.models["dataset"].source_path) == one


def test_explicit_sparse_format_mismatch_does_not_resolve_to_zero():
    """직접 고른 모델의 요청 형식 불일치를 상위 0번 모델 가져오기로 바꾸지 않습니다."""
    with TemporaryDirectory() as temporary:
        for selected_format, requested_format in (("BINARY", "TEXT"), ("TEXT", "BINARY")):
            root = Path(temporary) / selected_format
            if selected_format == "BINARY":
                zero = write_dataset(root)
                one = write_binary_model(root / "sparse" / "1")
            else:
                zero = write_binary_model(root / "sparse" / "0")
                one = write_dataset(root, index="1")
            inferred_root, layout = discover_sparse_model_with_dataset_root(root, requested_format)
            assert inferred_root == root and layout.source_path == zero
            runtime = RuntimeState()
            valid = make_job(one, input_format=selected_format)
            assert finish(valid, runtime) == "FINISHED", valid.error_message
            assert Path(valid.source_path) == one
            assert_explicit_selection_error(one, input_format=requested_format, expected_message=requested_format)


def test_explicit_sparse_missing_modern_pair_does_not_resolve_to_zero():
    """명시 모델의 rigs·frames 한쪽 누락 오류가 다른 모델 탐색으로 사라지지 않습니다."""
    records = {
        "rigs": "1 1 CAMERA 1\n",
        "frames": "1 1 1 0 0 0 0 0 0 1 CAMERA 1 1\n",
    }
    with TemporaryDirectory() as temporary:
        for present_stem, body in records.items():
            root = Path(temporary) / present_stem
            write_dataset(root)
            one = write_dataset(root, index="1")
            (one / f"{present_stem}.txt").write_text(body, encoding="utf-8")
            assert_explicit_selection_error(one, input_format="AUTO", expected_message="중 하나가 누락")


def test_explicit_sparse_partial_file_set_does_not_resolve_to_zero():
    """COLMAP 파일이 일부만 있는 명시 폴더도 0번 모델로 대신 가져오지 않습니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        zero = write_dataset(root)
        one = root / "sparse" / "1"
        one.mkdir()
        (one / "cameras.txt").write_bytes((zero / "cameras.txt").read_bytes())
        assert_explicit_selection_error(one, input_format="AUTO", expected_message="3파일 집합")


def test_bad_obj_and_cancellation_preserve_existing_dataset():
    """연결 오류·취소·중복 입력이 기존 데이터셋과 선택 상태를 바꾸지 않습니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = root / "first"
        write_dataset(first, mesh=False)
        second = root / "second"
        write_dataset(second)
        runtime = RuntimeState()
        assert finish(make_job(first, "first"), runtime) == "FINISHED"
        original = runtime.model_repository.models["first"]
        snapshot = runtime.model_catalog.snapshot()
        job = make_job(second, "second")
        while job.stage != IMPORT_STAGE_READ_COMPANION:
            assert job.step(runtime) == "RUNNING"
        assert job.step(runtime) == "RUNNING"
        assert job.request_cancel()
        assert job.step(runtime) == "CANCELLED"
        assert runtime.model_catalog.snapshot() == snapshot
        assert runtime.model_repository.models == {"first": original}
        assert not runtime.model_repository.companion_meshes
        (second / "model.OBJ").write_text("v 0 0 0\nf 1 2 3\n", encoding="utf-8")
        job = make_job(second, "second")
        assert finish(job, runtime) == "CANCELLED"
        assert runtime.model_catalog.snapshot() == snapshot
        assert runtime.model_repository.models == {"first": original}
        assert finish(make_job(first, "duplicate"), runtime) == "CANCELLED"
        assert runtime.model_repository.models == {"first": original}


def test_changed_obj_and_partial_gpu_failure_are_atomic():
    """저장 OBJ 지문 불일치와 표면 GPU 준비 실패가 부분 등록을 남기지 않습니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_dataset(root)
        obj = root / "model.OBJ"
        fingerprint = hashlib.sha256(obj.read_bytes()).hexdigest()
        obj.write_bytes(obj.read_bytes() + b"# changed\n")
        runtime = RuntimeState()
        job = make_job(root, companion_obj_path=str(obj), companion_expected_fingerprint=fingerprint)
        assert finish(job, runtime) == "CANCELLED"
        assert not runtime.model_repository.models
        calls = []
        discarded = []
        original_prepare = importer._prepare_gpu_batch_for_model
        original_discard = importer._discard_gpu_batch_for_model

        def fail_surface(model_id, model):
            """점군 준비 뒤 표면에서 GPU 오류를 주입합니다."""
            calls.append(model_id)
            if len(calls) == 2:
                raise RuntimeError("검증용 표면 GPU 오류")

        try:
            importer._prepare_gpu_batch_for_model = fail_surface
            importer._discard_gpu_batch_for_model = discarded.append
            job = create_import_job(root, model_id_factory=lambda: "dataset")
            assert finish(job, runtime) == "CANCELLED"
            assert calls == ["dataset", "dataset"] and discarded == ["dataset"]
            assert not runtime.model_repository.models
            assert not runtime.model_repository.companion_meshes
            assert not runtime.model_catalog.models
        finally:
            importer._prepare_gpu_batch_for_model = original_prepare
            importer._discard_gpu_batch_for_model = original_discard


def test_remove_and_clear_release_owned_mesh():
    """소유 데이터셋 삭제와 저장소 비우기가 연결 형상·출처를 함께 제거합니다."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_dataset(root)
        runtime = RuntimeState()
        assert finish(make_job(root), runtime) == "FINISHED"
        runtime.model_repository.remove_model("dataset")
        assert not runtime.model_repository.companion_meshes
        assert not runtime.model_repository.companion_sources
        runtime = RuntimeState()
        assert finish(make_job(root), runtime) == "FINISHED"
        runtime.model_repository.clear()
        assert not runtime.model_repository.companion_meshes
        assert not runtime.model_repository.companion_sources


def main():
    """연결 가져오기 경계 검증을 독립 Blender 프로세스에서 실행합니다."""
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("통과:", test.__name__)
    print(f"연결 가져오기 검증 {len(tests)}개 통과")


if __name__ == "__main__":
    main()
