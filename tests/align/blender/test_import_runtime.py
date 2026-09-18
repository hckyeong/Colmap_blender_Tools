"""실제 text sparse model의 Runtime 가져오기·원자성·빈 카탈로그를 검증합니다."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _write_text_sparse_model(directory: Path) -> None:
    """비연속 POINT3D_ID와 유효한 빈 트랙을 가진 최소 text 모델을 생성합니다."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.txt").write_text(
        "# Camera list\n1 SIMPLE_PINHOLE 640 480 500 320 240\n",
        encoding="utf-8",
    )
    (directory / "images.txt").write_text(
        "# Image list\n"
        "11 1 0 0 0 0 0 0 1 image_001.jpg\n"
        "0.0 0.0 -1\n",
        encoding="utf-8",
    )
    (directory / "points3D.txt").write_text(
        "# 3D point list\n"
        "101 0.0 0.0 0.0 255 0 0 0.1\n"
        "205 2.0 1.0 -3.0 0 128 255 0.2\n",
        encoding="utf-8",
    )


def _write_text_sparse_model_with_point_count(directory: Path, point_count: int) -> None:
    """표시 샘플링 단계 검증에 필요한 지정 개수의 유효 text 점 모델을 생성합니다."""
    if point_count < 1:
        raise ValueError("테스트 sparse model의 점 개수는 1 이상이어야 합니다.")
    _write_text_sparse_model(directory)
    point_rows = "".join(
        (
            f"{1_000 + index * 17} {index * 0.1:.6f} {index * 0.2:.6f} "
            f"{-index * 0.3:.6f} {index % 256} {(index * 3) % 256} "
            f"{(index * 7) % 256} 0.1\n"
        )
        for index in range(point_count)
    )
    (directory / "points3D.txt").write_text(
        "# 3D point list\n" + point_rows,
        encoding="utf-8",
    )


def _run_job_until_terminal(job, runtime) -> str:
    """단계형 ImportJob을 Blender TIMER와 같은 한 단계씩 끝까지 진행합니다."""
    result = "RUNNING"
    for _ in range(32):
        result = job.step(runtime)
        if result != "RUNNING":
            return result
    raise AssertionError("ImportJob이 제한된 단계 안에 종료되지 않았습니다.")


def test_catalog_is_empty_until_a_model_is_atomically_imported() -> None:
    """제품 기본 Runtime이 Demo 없이 비어 있고 첫 실제 모델만 Anchor가 되는지 확인합니다."""
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState
    from tools.align import importer

    runtime = RuntimeState()
    assert runtime.model_catalog.model_count == 0
    assert runtime.model_repository.models == {}
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            model_path = Path(temp_directory) / "first" / "sparse"
            _write_text_sparse_model(model_path)
            job = create_import_job(model_path, model_id_factory=lambda: "first-model")
            assert _run_job_until_terminal(job, runtime) == "FINISHED"
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu

    assert tuple(runtime.model_repository.models) == ("first-model",)
    record = runtime.model_catalog.models["first-model"]
    assert record.registration_status == "REGISTERED"
    assert record.is_anchor is True
    assert runtime.model_catalog.selected_model_id is None
    model = runtime.model_repository.models["first-model"]
    assert tuple(int(value) for value in model.points.point_ids) == (101, 205)
    assert tuple(float(value) for value in model.bounds_min) == (0.0, 0.0, -3.0)
    assert tuple(float(value) for value in model.bounds_max) == (2.0, 1.0, 0.0)
    assert runtime.model_repository.display_transforms["first-model"] is job.display_transform


def test_import_keeps_source_camera_and_points_while_storing_display_axis() -> None:
    """표시 축은 저장소에만 저장하고 원본 점·카메라·리그 데이터는 바꾸지 않는지 확인합니다."""
    from tools.align import importer
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            model_path = Path(temp_directory) / "axis" / "sparse"
            _write_text_sparse_model(model_path)
            job = create_import_job(
                model_path,
                axis_preset="CUSTOM",
                axis_forward="X",
                axis_up="Z",
                model_id_factory=lambda: "axis-model",
            )
            assert _run_job_until_terminal(job, runtime) == "FINISHED"
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu

    model = runtime.model_repository.models["axis-model"]
    transform = runtime.model_repository.display_transforms["axis-model"]
    assert transform is job.display_transform
    assert transform.preset_id == "CUSTOM"
    assert (transform.forward_axis, transform.up_axis) == ("X", "Z")
    assert tuple(tuple(float(value) for value in row) for row in model.points.positions) == (
        (0.0, 0.0, 0.0),
        (2.0, 1.0, -3.0),
    )
    image = model.images.image_records[11]
    assert tuple(float(value) for value in image.cam_from_world) == (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    assert model.rigs == {}
    assert model.frames == {}


def test_empty_import_path_is_rejected() -> None:
    """빈 문자열은 현재 작업 폴더가 아니라 사용자 입력 오류로 처리합니다."""
    from tools.align.importer import ImportJobError, create_import_job

    try:
        create_import_job("")
    except ImportJobError:
        return
    raise AssertionError("빈 가져오기 경로가 거부되지 않았습니다.")


def test_import_defaults_match_reference_time_budget() -> None:
    """기본 Import가 레코드 상한 없이 기준 Importer의 25ms 예산을 사용하는지 확인합니다."""
    from tools.align import importer

    assert importer.IMPORT_READ_MAX_RECORDS is None
    assert importer.IMPORT_READ_TIME_BUDGET_SECONDS == 0.025
    assert importer.IMPORT_FINGERPRINT_TIME_BUDGET_SECONDS == 0.025
    assert importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS == 0.025


def test_import_job_uses_incremental_chunks_and_validates_once() -> None:
    """ImportJob이 작은 레코드 예산으로 여러 tick을 쓰고 검증은 한 번만 수행하는지 확인합니다."""
    from tools.align import colmap_io, importer
    from tools.align.importer import IMPORT_STAGE_READ_MODEL, create_import_job
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    validation_count = 0
    read_tick_count = 0
    original_record_budget = importer.IMPORT_READ_MAX_RECORDS
    original_time_budget = importer.IMPORT_READ_TIME_BUDGET_SECONDS
    original_validate = colmap_io.validate_model
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        def _count_validation(model) -> None:
            """원래 검증을 실행하면서 호출 횟수를 기록합니다."""
            nonlocal validation_count
            validation_count += 1
            original_validate(model)

        importer.IMPORT_READ_MAX_RECORDS = 1
        importer.IMPORT_READ_TIME_BUDGET_SECONDS = 1.0
        colmap_io.validate_model = _count_validation
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            model_path = Path(temp_directory) / "incremental" / "sparse"
            _write_text_sparse_model(model_path)
            job = create_import_job(model_path, model_id_factory=lambda: "chunked-model")
            for _ in range(32):
                result = job.step(runtime)
                if job.stage == IMPORT_STAGE_READ_MODEL:
                    read_tick_count += 1
                if result != "RUNNING":
                    break
            else:
                raise AssertionError("증분 ImportJob이 제한된 tick 안에 끝나지 않았습니다.")
    finally:
        importer.IMPORT_READ_MAX_RECORDS = original_record_budget
        importer.IMPORT_READ_TIME_BUDGET_SECONDS = original_time_budget
        colmap_io.validate_model = original_validate
        importer._prepare_gpu_batch_for_model = original_prepare_gpu

    assert result == "FINISHED"
    assert read_tick_count >= 2
    assert validation_count == 1
    assert job.validation_performed is True


def test_import_job_snapshots_default_and_custom_display_point_limit() -> None:
    """ImportJob이 기본 25만과 사용자 상한을 Scene 변경과 독립된 작업 입력으로 보관하는지 확인합니다."""
    from tools.align.importer import create_import_job
    from tools.align.viewport.display_sampling import DEFAULT_DISPLAY_POINT_LIMIT

    with TemporaryDirectory() as temp_directory:
        model_path = Path(temp_directory) / "snapshot" / "sparse"
        _write_text_sparse_model(model_path)
        default_job = create_import_job(model_path)
        custom_job = create_import_job(model_path, display_point_limit=400_000)

    assert DEFAULT_DISPLAY_POINT_LIMIT == 250_000
    assert default_job.display_point_limit == DEFAULT_DISPLAY_POINT_LIMIT
    assert custom_job.display_point_limit == 400_000
    assert default_job.display_point_limit != custom_job.display_point_limit


def test_import_display_sampling_is_incremental_and_keeps_custom_snapshot() -> None:
    """표시 샘플링 TIMER 단계가 단조 진행 후 사용자 상한만큼 원자 등록하는지 확인합니다."""
    from tools.align import importer
    from tools.align.importer import (
        IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE,
        create_import_job,
    )
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    sample_progresses: list[float] = []
    sample_counts: list[int] = []
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    original_record_budget = importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS
    original_time_budget = importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = 1
        importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS = 1.0
        with TemporaryDirectory() as temp_directory:
            model_path = Path(temp_directory) / "sample-progress" / "sparse"
            _write_text_sparse_model_with_point_count(model_path, 17)
            job = create_import_job(
                model_path,
                display_point_limit=7,
                model_id_factory=lambda: "sample-progress-model",
            )
            for _ in range(128):
                stage_before_step = job.stage
                result = job.step(runtime)
                if stage_before_step == IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE:
                    sample_progresses.append(job.progress)
                    sample_counts.append(job.current_count)
                if result != "RUNNING":
                    break
            else:
                raise AssertionError("표시 샘플링 ImportJob이 제한된 TIMER tick 안에 끝나지 않았습니다.")
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu
        importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = original_record_budget
        importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS = original_time_budget

    assert result == "FINISHED"
    assert job.display_point_limit == 7
    assert len(sample_progresses) >= 2
    assert sample_progresses == sorted(sample_progresses)
    assert sample_counts == sorted(sample_counts)
    assert sample_counts[-1] == 17
    model = runtime.model_repository.models["sample-progress-model"]
    assert model.points.point_ids.size == 17
    assert model.points.display_indices.size == 7
    assert runtime.model_catalog.model_count == 1


def test_cancel_during_display_sampling_leaves_no_partial_model_or_gpu_batch() -> None:
    """표시 샘플링 중 취소가 후보를 정리하고 Repository·Catalog·GPU에 부분 결과를 남기지 않는지 확인합니다."""
    from tools.align import importer
    from tools.align.importer import (
        IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE,
        create_import_job,
    )
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    original_record_budget = importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS
    original_time_budget = importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = 1
        importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS = 1.0
        with TemporaryDirectory() as temp_directory:
            model_path = Path(temp_directory) / "sample-cancel" / "sparse"
            _write_text_sparse_model_with_point_count(model_path, 31)
            job = create_import_job(
                model_path,
                display_point_limit=11,
                model_id_factory=lambda: "sample-cancel-model",
            )
            for _ in range(64):
                assert job.step(runtime) == "RUNNING"
                if job.stage == IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE:
                    break
            else:
                raise AssertionError("취소할 표시 샘플링 단계에 도달하지 못했습니다.")
            builder = job.display_index_builder
            assert builder is not None
            assert job.request_cancel() is True
            assert job.step(runtime) == "CANCELLED"
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu
        importer.IMPORT_DISPLAY_SAMPLE_MAX_RECORDS = original_record_budget
        importer.IMPORT_DISPLAY_SAMPLE_TIME_BUDGET_SECONDS = original_time_budget

    assert builder.is_cancelled is True
    assert runtime.model_catalog.model_count == 0
    assert runtime.model_repository.models == {}
    assert runtime.model_repository.display_transforms == {}
    assert runtime.gpu_batches == {}
    assert job.staged_model is None


def test_duplicate_source_creates_reader_and_independent_pending_layers() -> None:
    """같은 원본을 매번 다시 읽어 고유 이름과 독립 배열·변환의 대기 레이어로 등록합니다."""
    from tools.align import colmap_io, importer
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    reader_creation_count = 0
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    original_reader_factory = colmap_io.create_incremental_sparse_model_reader
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            selected_root = Path(temp_directory) / "duplicate"
            model_path = selected_root / "sparse" / "0"
            _write_text_sparse_model(model_path)
            first = create_import_job(selected_root, model_id_factory=lambda: "first-model")
            assert _run_job_until_terminal(first, runtime) == "FINISHED"
            runtime.model_repository.set_alignment_transform(
                "first-model",
                create_sim3_transform(((1, 0, 0), (0, 1, 0), (0, 0, 1)), 2.0, (3, 4, 5)),
            )
            first_model = runtime.model_repository.models["first-model"]
            first_transform = runtime.model_repository.display_transforms["first-model"]
            first_alignment = runtime.model_repository.alignment_transforms["first-model"]

            def _count_reader_factory(*args, **kwargs):
                """같은 경로를 다시 읽는 실제 reader 생성 횟수를 기록합니다."""
                nonlocal reader_creation_count
                reader_creation_count += 1
                return original_reader_factory(*args, **kwargs)

            colmap_io.create_incremental_sparse_model_reader = _count_reader_factory
            _write_text_sparse_model_with_point_count(model_path, 3)
            duplicate = create_import_job(selected_root, model_id_factory=lambda: "duplicate-model")
            assert _run_job_until_terminal(duplicate, runtime) == "FINISHED"
            third = create_import_job(selected_root, model_id_factory=lambda: "third-model")
            assert _run_job_until_terminal(third, runtime) == "FINISHED"
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu
        colmap_io.create_incremental_sparse_model_reader = original_reader_factory

    assert reader_creation_count == 2
    assert runtime.model_catalog.model_count == 3
    assert tuple(runtime.model_repository.models) == ("first-model", "duplicate-model", "third-model")
    assert runtime.model_repository.models["first-model"].source_path == model_path.resolve()
    assert runtime.model_repository.models["first-model"].display_name == "duplicate"
    assert runtime.model_repository.models["first-model"] is first_model
    assert first_model.points.point_ids.size == 2
    assert runtime.model_repository.display_transforms["first-model"] is first_transform
    assert runtime.model_repository.alignment_transforms["first-model"] is first_alignment
    assert runtime.model_repository.model_id_for_source_path(model_path) == "first-model"
    for model_id, name in (("duplicate-model", "duplicate_2"), ("third-model", "duplicate_3")):
        model = runtime.model_repository.models[model_id]
        assert model.display_name == name
        assert model.points.point_ids.size == 3
        assert model.points.positions is not first_model.points.positions
        record = runtime.model_catalog.models[model_id]
        assert record.registration_status == "PENDING" and not record.is_anchor
    assert runtime.model_catalog.selected_model_id == "third-model"


def test_import_records_inferred_dataset_root_from_sparse_zero_selection() -> None:
    """sparse/0을 직접 선택해도 저장 메타데이터에는 Dataset Root가 기록되는지 확인합니다."""
    from tools.align import importer
    from tools.align.importer import create_import_job
    from tools.align.model_repository import canonical_source_path
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            dataset_root = Path(temp_directory) / "Component 1"
            model_path = dataset_root / "sparse" / "0"
            _write_text_sparse_model(model_path)
            job = create_import_job(model_path, model_id_factory=lambda: "inferred-root-model")
            assert _run_job_until_terminal(job, runtime) == "FINISHED"

            metadata = runtime.model_repository.source_metadata_for_model_id(
                "inferred-root-model"
            )
            assert Path(job.dataset_root) == dataset_root.resolve()
            assert metadata.selected_root_absolute == canonical_source_path(dataset_root)
            assert metadata.sparse_path_absolute == canonical_source_path(model_path)
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu


def test_second_import_selects_pending_layer_and_id_collision_is_atomic() -> None:
    """대기 레이어 선택을 유지하고 기존 ID 충돌은 실제 GPU 정리 경로에서도 캐시를 보존합니다."""
    from unittest.mock import patch
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState
    from tools.align import importer
    from tools.align.viewport import renderer

    runtime = RuntimeState()
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            first_path = root / "one" / "sparse"
            second_path = root / "two" / "sparse"
            _write_text_sparse_model(first_path)
            _write_text_sparse_model(second_path)
            first = create_import_job(first_path, model_id_factory=lambda: "first-model")
            second = create_import_job(second_path, model_id_factory=lambda: "second-model")
            assert _run_job_until_terminal(first, runtime) == "FINISHED"
            assert _run_job_until_terminal(second, runtime) == "FINISHED"
            catalog_before = runtime.model_catalog.snapshot()
            repository_before = dict(runtime.model_repository.models)
            transforms_before = dict(runtime.model_repository.display_transforms)
            cache_key = ("first-model", 1, 1, "기존 표시 표본")
            cached_batch = object()
            runtime.gpu_batches[cache_key] = cached_batch
            duplicate = create_import_job(first_path, model_id_factory=lambda: "first-model")
            with patch.object(renderer, "RUNTIME", runtime), patch.object(
                importer, "_prepare_gpu_batch_for_model",
            ) as prepare_gpu:
                assert _run_job_until_terminal(duplicate, runtime) == "CANCELLED"
                prepare_gpu.assert_not_called()
                assert runtime.gpu_batches == {cache_key: cached_batch}
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu

    second_record = runtime.model_catalog.models["second-model"]
    assert second_record.registration_status == "PENDING"
    assert second_record.is_anchor is False
    assert runtime.model_catalog.selected_model_id == "second-model"
    assert runtime.model_repository.models == repository_before
    assert tuple(runtime.model_repository.display_transforms) == tuple(transforms_before)
    assert all(
        runtime.model_repository.display_transforms[model_id] is transform
        for model_id, transform in transforms_before.items()
    )
    assert runtime.model_catalog.snapshot() == catalog_before
    assert "ID" in duplicate.error_message
    assert runtime.model_repository.models["second-model"].display_name == "sparse_2"


def test_mesh_id_collision_preserves_gpu_cache_and_embedded_asset() -> None:
    """독립 OBJ의 ID 충돌이 기존 GPU 캐시·내장 메시를 보존하고 후보 자산을 남기지 않습니다."""
    from unittest.mock import patch
    import bpy
    from tools.align import mesh_importer
    from tools.align.mesh_io.embedded_obj_store import (
        find_embedded_obj_mesh,
        remove_embedded_obj_mesh,
    )
    from tools.align.state import RuntimeState
    from tools.align.viewport import renderer

    runtime = RuntimeState()
    asset_uuid = ""
    try:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.obj"
            path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
            first = mesh_importer.create_mesh_import_job(
                path, model_id_factory=lambda: "mesh-existing", prepare_gpu=False,
            )
            assert _run_job_until_terminal(first, runtime) == "FINISHED"
            model = runtime.model_repository.models["mesh-existing"]
            asset_uuid = model.embedded_asset_uuid
            original_mesh = find_embedded_obj_mesh(asset_uuid)
            mesh_pointers = {mesh.as_pointer() for mesh in bpy.data.meshes}
            catalog_before = runtime.model_catalog.snapshot()
            cache_key = ("mesh-existing", 1, 1, "기존 표시 표본")
            cached_batch = object()
            runtime.gpu_batches[cache_key] = cached_batch
            with patch.object(renderer, "RUNTIME", runtime), patch.object(
                mesh_importer, "_prepare_gpu_batch_for_model",
            ) as prepare_gpu:
                collision = mesh_importer.create_mesh_import_job(
                    path, model_id_factory=lambda: "mesh-existing",
                )
                assert _run_job_until_terminal(collision, runtime) == "CANCELLED"
                assert "ID" in collision.error_message
                prepare_gpu.assert_not_called()
                assert runtime.gpu_batches == {cache_key: cached_batch}
            assert runtime.model_repository.models == {"mesh-existing": model}
            assert runtime.model_catalog.snapshot() == catalog_before
            assert find_embedded_obj_mesh(asset_uuid) is original_mesh
            assert {mesh.as_pointer() for mesh in bpy.data.meshes} == mesh_pointers
    finally:
        if asset_uuid:
            remove_embedded_obj_mesh(asset_uuid, expected_model_id="mesh-existing")


def test_cancel_and_gpu_failure_leave_catalog_and_repository_untouched() -> None:
    """취소와 GPU 준비 실패가 Data Layers·저장소·GPU 캐시를 부분 변경하지 않는지 확인합니다."""
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState
    from tools.align import importer

    runtime = RuntimeState()
    with TemporaryDirectory() as temp_directory:
        model_path = Path(temp_directory) / "sparse"
        _write_text_sparse_model(model_path)
        cancelled = create_import_job(model_path, model_id_factory=lambda: "cancelled-model")
        assert cancelled.request_cancel() is True
        assert cancelled.step(runtime) == "CANCELLED"
        assert runtime.model_catalog.model_count == 0
        assert runtime.model_repository.models == {}
        assert runtime.model_repository.display_transforms == {}

        original_prepare_gpu = importer._prepare_gpu_batch_for_model
        try:
            def _raise_gpu_error(_model_id, _model) -> None:
                """GPU 배치 실패 시 원자성 검사를 위해 의도적으로 오류를 발생시킵니다."""
                raise RuntimeError("테스트 GPU 준비 실패")

            importer._prepare_gpu_batch_for_model = _raise_gpu_error
            failed = create_import_job(model_path, model_id_factory=lambda: "failed-model")
            assert _run_job_until_terminal(failed, runtime) == "CANCELLED"
        finally:
            importer._prepare_gpu_batch_for_model = original_prepare_gpu

    assert runtime.model_catalog.model_count == 0
    assert runtime.model_repository.models == {}
    assert runtime.model_repository.display_transforms == {}
    assert runtime.gpu_batches == {}
    assert "테스트 GPU 준비 실패" in failed.error_message


def test_import_rejects_source_changed_after_initial_fingerprint() -> None:
    """가져오기 전 지문 이후 원본이 바뀌면 Runtime 확정 없이 실패하는지 확인합니다."""
    from tools.align import importer
    from tools.align.importer import IMPORT_STAGE_READ_MODEL, create_import_job
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        with TemporaryDirectory() as temp_directory:
            model_path = Path(temp_directory) / "changed" / "sparse" / "0"
            _write_text_sparse_model(model_path)
            job = create_import_job(model_path, model_id_factory=lambda: "changed-model")
            for _ in range(16):
                assert job.step(runtime) == "RUNNING"
                if job.stage == IMPORT_STAGE_READ_MODEL:
                    break
            else:
                raise AssertionError("가져오기 전 source 지문 단계가 끝나지 않았습니다.")

            cameras_path = model_path / "cameras.txt"
            cameras_path.write_text(
                cameras_path.read_text(encoding="utf-8") + "# 원본 변경\n",
                encoding="utf-8",
            )
            assert _run_job_until_terminal(job, runtime) == "CANCELLED"
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu

    assert runtime.model_catalog.model_count == 0
    assert runtime.model_repository.models == {}
    assert runtime.model_repository.display_transforms == {}
    assert "변경되었습니다" in job.error_message


def test_import_ui_exposes_folder_selection_progress_and_cancel() -> None:
    """파일 탐색기 축 UI와 상세 N 패널 진행·취소 UI를 정적으로 확인합니다."""
    from tools.align.operators.import_model import (
        COLMAPALIGN_OT_cancel_import_model,
        COLMAPALIGN_OT_import_model,
        COLMAPALIGN_OT_import_model_job,
    )
    from tools.align.display_axes import AXIS_PRESETS
    from tools.align.ui.panels import COLMAPALIGN_PT_data_layers

    source = inspect.getsource(COLMAPALIGN_PT_data_layers.draw)
    operator_source = inspect.getsource(COLMAPALIGN_OT_import_model)
    assert "colmap_align.import_model" in source
    assert "colmap_align.cancel_import_model" in source
    assert "layout.progress" in source
    assert "import_status_view" in source
    assert "세부 작업:" in source
    assert "처리량:" in source
    assert "경과 시간:" in source
    assert "선택 폴더:" in source
    assert "입력 형식:" in source
    assert "표시 축:" in source
    assert "import_dataset_root" not in source
    assert "import_input_format" not in source
    assert "불러온 데이터 레이어가 없습니다." in source
    assert "fileselect_add" in operator_source
    assert "filter_folder" in operator_source
    assert "def draw" in operator_source
    assert "input_format" in operator_source
    assert "display_point_limit" in operator_source
    assert "Viewport Points" in operator_source
    assert "Point Limit" in operator_source
    assert "axis_preset" in operator_source
    assert "axis_forward" in operator_source
    assert "axis_up" in operator_source
    assert tuple(preset.label for preset in AXIS_PRESETS) == (
        "RealityCapture (RC)",
        "Blender",
        "Maya",
        "Houdini",
        "Custom",
    )
    assert COLMAPALIGN_OT_import_model.bl_label == "COLMAP 모델 불러오기"
    assert "INTERNAL" in COLMAPALIGN_OT_import_model_job.bl_options
    assert COLMAPALIGN_OT_cancel_import_model.bl_label == "가져오기 취소"


def test_import_status_view_exposes_detailed_panel_contract() -> None:
    """ImportJob 진행값이 N 패널의 세부 단계와 입력 정보로 변환되는지 확인합니다."""
    from tools.align.importer import (
        IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE,
        IMPORT_STAGE_READ_MODEL,
        create_import_job,
    )
    from tools.align.ui.import_status import compact_path, import_status_view
    from tools.align.viewport.renderer import _draw_post_pixel

    dataset_root = Path("C:/very/long/dataset/root")
    job = create_import_job(dataset_root)
    job.stage = IMPORT_STAGE_READ_MODEL
    job.progress = 0.48
    job.current_count = 25
    job.total_count = 100
    job.reader_stage = "POINTS3D"
    job.selected_format = "text"
    job.layout = SimpleNamespace(layout_kind="legacy")
    job.started_at = perf_counter() - 1.0
    view = import_status_view(job)
    assert view.fraction == 0.48
    assert view.percentage_text == "48%"
    assert view.stage_text == "COLMAP 데이터 읽기"
    assert view.detail_stage_text == "3D 점과 관측 트랙 읽기"
    assert view.processed_text == "25 / 100 레코드"
    assert view.elapsed_text.endswith("초")
    assert view.dataset_text == str(dataset_root)
    assert view.source_text == ""
    assert view.format_text == "Text / Legacy"
    assert view.axis_text.startswith("RealityCapture (RC)")
    job.stage = IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE
    job.progress = 0.87
    job.current_count = 25
    job.total_count = 100
    job.display_point_limit = 400_000
    sampling_view = import_status_view(job)
    assert sampling_view.stage_text == "뷰포트 표시 점 선택"
    assert sampling_view.detail_stage_text == "표시 상한: 모델당 400,000"
    assert sampling_view.processed_text == "25 / 100 점"
    job.current_count = 12_345
    job.total_count = 0
    assert import_status_view(job).processed_text == (
        "12,345 점 처리됨 (전체 수 확인 중)"
    )
    assert compact_path("x" * 100, maximum=12) == "…" + ("x" * 11)
    draw_source = inspect.getsource(_draw_post_pixel)
    assert "import_overlay" not in draw_source
    assert "draw_active_import_overlay" not in draw_source


def run_all() -> None:
    """실제 COLMAP text 가져오기와 Runtime 원자성 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_catalog_is_empty_until_a_model_is_atomically_imported()
    test_import_keeps_source_camera_and_points_while_storing_display_axis()
    test_empty_import_path_is_rejected()
    test_import_defaults_match_reference_time_budget()
    test_import_job_uses_incremental_chunks_and_validates_once()
    test_import_job_snapshots_default_and_custom_display_point_limit()
    test_import_display_sampling_is_incremental_and_keeps_custom_snapshot()
    test_cancel_during_display_sampling_leaves_no_partial_model_or_gpu_batch()
    test_duplicate_source_creates_reader_and_independent_pending_layers()
    test_import_records_inferred_dataset_root_from_sparse_zero_selection()
    test_second_import_selects_pending_layer_and_id_collision_is_atomic()
    test_mesh_id_collision_preserves_gpu_cache_and_embedded_asset()
    test_cancel_and_gpu_failure_leave_catalog_and_repository_untouched()
    test_import_rejects_source_changed_after_initial_fingerprint()
    test_import_ui_exposes_folder_selection_progress_and_cancel()
    test_import_status_view_exposes_detailed_panel_contract()
    print("test_import_runtime: 통과")


if __name__ == "__main__":
    run_all()
