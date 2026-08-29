"""COLMAP text·binary sparse model 파서와 무결성 검증을 Blender에서 확인합니다."""

from __future__ import annotations

from pathlib import Path
import struct
import sys
import tempfile

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender Python 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _write_text_model(directory: Path) -> None:
    """리그·프레임·관측 트랙을 포함한 작은 정상 text sparse model을 만듭니다."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.txt").write_text(
        "# Camera list\n"
        "5 PINHOLE 640 480 500.0 500.0 320.0 240.0\n"
        "6 PINHOLE 640 480 510.0 510.0 320.0 240.0\n",
        encoding="utf-8",
    )
    (directory / "images.txt").write_text(
        "# Image list\n"
        "7 1 0 0 0 0 0 0 5 images/a.jpg\n"
        "10.0 20.0 42 30.0 40.0 -1\n"
        "8 1 0 0 0 0 0 0 6 images/b.jpg\n"
        "11.0 21.0 42\n",
        encoding="utf-8",
    )
    (directory / "points3D.txt").write_text(
        "# Point list\n42 1.0 2.0 3.0 10 20 30 0.5 7 0 8 0\n",
        encoding="utf-8",
    )
    (directory / "rigs.txt").write_text(
        "9 2 CAMERA 5 CAMERA 6 1 1 0 0 0 0.1 0.2 0.3\n",
        encoding="utf-8",
    )
    (directory / "frames.txt").write_text(
        "11 9 1 0 0 0 0 0 0 2 CAMERA 5 7 CAMERA 6 8\n",
        encoding="utf-8",
    )


def _write_binary_model(directory: Path) -> None:
    """공식 little-endian 필드 순서를 따르는 작은 정상 binary sparse model을 만듭니다."""
    directory.mkdir(parents=True, exist_ok=True)
    cameras = struct.pack("<Q", 2)
    cameras += struct.pack("<IiQQ4d", 5, 1, 640, 480, 500.0, 500.0, 320.0, 240.0)
    cameras += struct.pack("<IiQQ4d", 6, 1, 640, 480, 510.0, 510.0, 320.0, 240.0)
    (directory / "cameras.bin").write_bytes(cameras)

    images = struct.pack("<Q", 2)
    images += struct.pack("<I7dI", 7, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5)
    images += b"images/a.jpg\0"
    images += struct.pack("<Q", 2)
    images += struct.pack("<ddQ", 10.0, 20.0, 42)
    images += struct.pack("<ddQ", 30.0, 40.0, np.iinfo(np.uint64).max)
    images += struct.pack("<I7dI", 8, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6)
    images += b"images/b.jpg\0"
    images += struct.pack("<Q", 1)
    images += struct.pack("<ddQ", 11.0, 21.0, 42)
    (directory / "images.bin").write_bytes(images)

    points = struct.pack("<Q", 1)
    points += struct.pack("<Q3d3BdQ", 42, 1.0, 2.0, 3.0, 10, 20, 30, 0.5, 2)
    points += struct.pack("<II", 7, 0)
    points += struct.pack("<II", 8, 0)
    (directory / "points3D.bin").write_bytes(points)

    rigs = struct.pack("<Q", 1)
    rigs += struct.pack("<IIiI", 9, 2, 0, 5)
    rigs += struct.pack("<iIB7d", 0, 6, 1, 1.0, 0.0, 0.0, 0.0, 0.1, 0.2, 0.3)
    (directory / "rigs.bin").write_bytes(rigs)

    frames = struct.pack("<Q", 1)
    frames += struct.pack("<II7dI", 11, 9, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2)
    frames += struct.pack("<iIQ", 0, 5, 7)
    frames += struct.pack("<iIQ", 0, 6, 8)
    (directory / "frames.bin").write_bytes(frames)


def _write_many_points_text_model(directory: Path, point_count: int) -> None:
    """다중 TIMER 청크 검사용 text points3D 레코드를 만듭니다."""
    if point_count < 1:
        raise ValueError("테스트 점 개수는 1 이상이어야 합니다.")
    _write_text_model(directory)
    records = ["42 1.0 2.0 3.0 10 20 30 0.5 7 0 8 0"]
    records.extend(
        f"{1000 + index} {float(index)} 2.0 3.0 1 2 3 0.25"
        for index in range(1, point_count)
    )
    (directory / "points3D.txt").write_text("\n".join(records) + "\n", encoding="utf-8")


def _write_many_points_binary_model(directory: Path, point_count: int) -> None:
    """다중 TIMER 청크 검사용 binary points3D 레코드를 만듭니다."""
    if point_count < 1:
        raise ValueError("테스트 점 개수는 1 이상이어야 합니다.")
    _write_binary_model(directory)
    points = struct.pack("<Q", point_count)
    points += struct.pack("<Q3d3BdQ", 42, 1.0, 2.0, 3.0, 10, 20, 30, 0.5, 2)
    points += struct.pack("<II", 7, 0)
    points += struct.pack("<II", 8, 0)
    for index in range(1, point_count):
        points += struct.pack(
            "<Q3d3BdQ",
            1000 + index,
            float(index),
            2.0,
            3.0,
            1,
            2,
            3,
            0.25,
            0,
        )
    (directory / "points3D.bin").write_bytes(points)


def _write_large_points2d_line_text_model(directory: Path, point2d_count: int) -> None:
    """1 MiB보다 큰 정상 images.txt points2D 한 줄을 가진 모델을 만듭니다."""
    if point2d_count < 2:
        raise ValueError("테스트 points2D 개수는 2 이상이어야 합니다.")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.txt").write_text(
        "1 PINHOLE 640 480 500.0 500.0 320.0 240.0\n",
        encoding="utf-8",
    )
    points_line = "0 0 1 " + ("0 0 -1 " * (point2d_count - 1))
    if len(points_line) <= 1_048_576:
        raise AssertionError("대형 points2D 줄 fixture가 1 MiB를 넘지 않습니다.")
    (directory / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 image.jpg\n" + points_line + "\n",
        encoding="utf-8",
    )
    (directory / "points3D.txt").write_text(
        "1 1.0 2.0 3.0 1 2 3 0.25 1 0\n",
        encoding="utf-8",
    )


def _advance_to_points_stage(reader: object) -> None:
    """공개 증분 reader를 points3D 단계 직전까지 작은 청크로 진행합니다."""
    from tools.align.colmap_io import STAGE_POINTS3D

    for _ in range(128):
        if reader.stage == STAGE_POINTS3D:
            return
        reader.step(max_records=1)
    raise AssertionError("증분 reader가 points3D 단계에 도달하지 못했습니다.")


def _expect_error(error_type: type[Exception], callback: object) -> None:
    """지정 예외가 발생하지 않으면 테스트 실패로 처리합니다."""
    try:
        callback()  # type: ignore[operator]
    except error_type:
        return
    raise AssertionError(f"{error_type.__name__} 예외가 발생해야 합니다.")


def _assert_model_contract(model: object, expected_format: str) -> None:
    """파싱 결과의 핵심 필드, dtype, 연속성 및 읽기 전용 계약을 확인합니다."""
    assert model.source_format == expected_format
    assert model.model_id == ""
    assert model.geometry_revision == 0
    assert tuple(model.cameras) == (5, 6)
    assert tuple(model.images.image_records) == (7, 8)
    assert tuple(model.rigs) == (9,)
    assert tuple(model.frames) == (11,)
    assert np.array_equal(model.points.point_ids, np.asarray([42], dtype=np.uint64))
    assert np.array_equal(model.points.positions, np.asarray([[1.0, 2.0, 3.0]]))
    assert np.array_equal(model.points.colors, np.asarray([[10, 20, 30]], dtype=np.uint8))
    assert np.array_equal(model.points.errors, np.asarray([0.5]))
    assert np.array_equal(model.points.track_offsets, np.asarray([0, 2], dtype=np.uint64))
    assert np.array_equal(model.points.track_image_ids, np.asarray([7, 8], dtype=np.uint64))
    assert np.array_equal(model.points.track_point2d_indices, np.asarray([0, 0], dtype=np.uint32))
    assert np.array_equal(model.images.point2d_offsets, np.asarray([0, 2, 3], dtype=np.uint64))
    assert model.rigs[9].sensors[1].sensor_from_rig is not None
    assert np.array_equal(
        model.rigs[9].sensors[1].sensor_from_rig,
        np.asarray([1.0, 0.0, 0.0, 0.0, 0.1, 0.2, 0.3]),
    )
    assert np.array_equal(model.points.display_indices, np.asarray([0], dtype=np.uint32))
    assert np.array_equal(model.bounds_min, np.asarray([1.0, 2.0, 3.0]))
    assert np.array_equal(model.bounds_max, np.asarray([1.0, 2.0, 3.0]))
    for array_value in (
        model.points.point_ids,
        model.points.positions,
        model.points.colors,
        model.points.errors,
        model.points.track_offsets,
        model.points.track_image_ids,
        model.points.track_point2d_indices,
        model.points.display_indices,
        model.images.point2d_offsets,
        model.images.point2d_xy,
        model.images.point2d_point3d_ids,
    ):
        assert array_value.flags.c_contiguous
        assert not array_value.flags.writeable


def test_text_and_binary_sparse_models_have_same_contract() -> None:
    """동일 sparse model의 text·binary 입력이 같은 CPU 데이터 계약을 만드는지 확인합니다."""
    from tools.align.colmap_io import load_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        text_directory = root / "text_sparse"
        binary_directory = root / "binary_sparse"
        _write_text_model(text_directory)
        _write_binary_model(binary_directory)
        text_model = load_sparse_model(text_directory)
        binary_model = load_sparse_model(binary_directory)
        _assert_model_contract(text_model, "text")
        _assert_model_contract(binary_model, "binary")
        assert np.array_equal(text_model.points.positions, binary_model.points.positions)
        assert np.array_equal(text_model.images.point2d_xy, binary_model.images.point2d_xy)
        assert np.array_equal(
            text_model.images.point2d_point3d_ids,
            binary_model.images.point2d_point3d_ids,
        )


def test_camera_model_table_covers_supported_colmap_ids() -> None:
    """카메라 모델 ID·이름·파라미터 수가 공식 지원 표와 일치하는지 확인합니다."""
    from tools.align.colmap_io.camera_models import (
        CAMERA_MODELS,
        camera_model_from_id,
        camera_model_from_name,
    )

    expected_parameter_counts = {
        0: 3,
        1: 4,
        2: 4,
        3: 5,
        4: 8,
        5: 8,
        6: 12,
        7: 5,
        8: 4,
        9: 5,
        10: 12,
        11: 16,
        12: 4,
        13: 5,
        14: 3,
        15: 4,
        16: 6,
        17: 2,
    }
    assert {model.model_id: model.parameter_count for model in CAMERA_MODELS} == expected_parameter_counts
    for model in CAMERA_MODELS:
        assert camera_model_from_id(model.model_id) == model
        assert camera_model_from_name(model.name) == model


def test_discovery_matches_reference_format_selection() -> None:
    """AUTO의 BIN 우선과 명시 형식 및 반대 형식 partial 허용을 확인합니다."""
    from tools.align.colmap_io import ColmapDiscoveryError, discover_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        complete_directory = root / "both_sparse"
        _write_text_model(complete_directory)
        _write_binary_model(complete_directory)
        layout = discover_sparse_model(complete_directory)
        assert layout.selected_format == "binary"
        assert layout.rigs_path is not None and layout.rigs_path.suffix == ".bin"
        assert layout.frames_path is not None and layout.frames_path.suffix == ".bin"
        text_layout = discover_sparse_model(complete_directory, input_format="TEXT")
        assert text_layout.selected_format == "text"

        partial_directory = root / "partial_sparse"
        _write_text_model(partial_directory)
        (partial_directory / "cameras.bin").write_bytes((complete_directory / "cameras.bin").read_bytes())
        partial_layout = discover_sparse_model(partial_directory)
        assert partial_layout.selected_format == "text"
        _expect_error(
            ColmapDiscoveryError,
            lambda: discover_sparse_model(partial_directory, input_format="BINARY"),
        )


def test_discovery_prioritizes_standard_sparse_zero_and_rejects_nonstandard_ambiguity() -> None:
    """기준 Importer처럼 sparse/0을 우선하고 비표준 다중 후보만 거부합니다."""
    from tools.align.colmap_io import ColmapDiscoveryError, discover_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        selected_root = root / "final"
        model_directory = selected_root / "sparse" / "0"
        _write_text_model(model_directory)
        layout = discover_sparse_model(selected_root)
        assert layout.source_path == model_directory.resolve()
        assert layout.selected_format == "text"

        second_directory = selected_root / "sparse" / "1"
        _write_binary_model(second_directory)
        selected_layout = discover_sparse_model(selected_root)
        assert selected_layout.source_path == model_directory.resolve()

        ambiguous_root = root / "ambiguous"
        _write_text_model(ambiguous_root / "one" / "model")
        _write_binary_model(ambiguous_root / "two" / "model")
        _expect_error(ColmapDiscoveryError, lambda: discover_sparse_model(ambiguous_root))

        deep_root = root / "deep"
        _write_text_model(deep_root / "one" / "two" / "model")
        _expect_error(ColmapDiscoveryError, lambda: discover_sparse_model(deep_root))


def test_discovery_infers_dataset_root_from_standard_subfolders() -> None:
    """표준 Dataset의 sparse·images 하위 선택에서 같은 Dataset Root를 복원하는지 확인합니다."""
    from tools.align.colmap_io import discover_sparse_model_with_dataset_root

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        dataset_root = root / "Component 1"
        model_directory = dataset_root / "sparse" / "0"
        nested_images_directory = dataset_root / "images" / "set_a"
        other_dataset_directory = dataset_root / "dense"
        _write_text_model(model_directory)
        nested_images_directory.mkdir(parents=True)
        other_dataset_directory.mkdir()

        for selected_path in (
            dataset_root,
            dataset_root / "sparse",
            model_directory,
            dataset_root / "images",
            nested_images_directory,
            other_dataset_directory,
        ):
            inferred_root, layout = discover_sparse_model_with_dataset_root(selected_path)
            assert inferred_root == dataset_root.resolve()
            assert layout.source_path == model_directory.resolve()

        no_images_root = root / "No Images"
        no_images_model = no_images_root / "sparse" / "0"
        _write_text_model(no_images_model)
        inferred_root, layout = discover_sparse_model_with_dataset_root(no_images_model)
        assert inferred_root == no_images_root.resolve()
        assert layout.source_path == no_images_model.resolve()

        standalone_model = root / "standalone_model"
        _write_text_model(standalone_model)
        inferred_root, layout = discover_sparse_model_with_dataset_root(standalone_model)
        assert inferred_root == standalone_model.resolve()
        assert layout.source_path == standalone_model.resolve()


def test_validation_rejects_broken_bidirectional_track_reference() -> None:
    """points3D 트랙과 images points2D가 불일치하면 전체 model load가 실패하는지 확인합니다."""
    from tools.align.colmap_io import ColmapValidationError, load_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "broken_sparse"
        _write_text_model(model_directory)
        (model_directory / "points3D.txt").write_text(
            "42 1.0 2.0 3.0 10 20 30 0.5 7 1\n",
            encoding="utf-8",
        )
        _expect_error(ColmapValidationError, lambda: load_sparse_model(model_directory))


def test_binary_parser_rejects_truncation_and_trailing_bytes() -> None:
    """binary 조기 EOF와 선언 레코드 뒤 trailing data가 명확히 거부되는지 확인합니다."""
    from tools.align.colmap_io import ColmapParseError, load_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "truncated_sparse"
        _write_binary_model(model_directory)
        points_path = model_directory / "points3D.bin"
        points_path.write_bytes(points_path.read_bytes()[:-1])
        _expect_error(ColmapParseError, lambda: load_sparse_model(model_directory))

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "trailing_sparse"
        _write_binary_model(model_directory)
        cameras_path = model_directory / "cameras.bin"
        cameras_path.write_bytes(cameras_path.read_bytes() + b"\0")
        _expect_error(ColmapParseError, lambda: load_sparse_model(model_directory))


def test_text_parser_rejects_unknown_camera_model() -> None:
    """추측으로 파라미터 수를 정하지 않고 알 수 없는 카메라 모델을 거부하는지 확인합니다."""
    from tools.align.colmap_io import ColmapParseError, load_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "unknown_model_sparse"
        _write_text_model(model_directory)
        (model_directory / "cameras.txt").write_text(
            "5 UNKNOWN_MODEL 640 480 500 500 320 240\n",
            encoding="utf-8",
        )
        _expect_error(ColmapParseError, lambda: load_sparse_model(model_directory))


def test_text_readers_preserve_image_names_with_spaces() -> None:
    """일반·증분 text 파서가 공백이 포함된 이미지 이름 전체를 보존하는지 확인합니다."""
    from tools.align.colmap_io import (
        create_incremental_sparse_model_reader,
        discover_sparse_model,
        read_images_text,
    )

    expected_name = "images/반가사유상 촬영 (28).jpg"
    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "spaced_image_name_sparse"
        _write_text_model(model_directory)
        images_path = model_directory / "images.txt"
        images_path.write_text(
            images_path.read_text(encoding="utf-8").replace("images/a.jpg", expected_name),
            encoding="utf-8",
        )
        layout = discover_sparse_model(model_directory)
        images = read_images_text(layout.images_path)
        assert images.image_records[7].name == expected_name

        reader = create_incremental_sparse_model_reader(layout)
        try:
            while not reader.is_completed:
                reader.step(max_records=1)
            assert reader.model.images.image_records[7].name == expected_name
        finally:
            reader.close()


def test_incremental_reader_handles_binary_and_text_in_multiple_chunks() -> None:
    """binary·text points3D가 여러 TIMER 청크에서 한 레코드씩 진행되는지 확인합니다."""
    from tools.align.colmap_io import (
        STAGE_POINTS3D,
        create_incremental_sparse_model_reader,
        discover_sparse_model,
    )

    point_count = 513
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        text_directory = root / "many_text_sparse"
        binary_directory = root / "many_binary_sparse"
        _write_many_points_text_model(text_directory, point_count)
        _write_many_points_binary_model(binary_directory, point_count)
        for model_directory, expected_total in (
            (text_directory, None),
            (binary_directory, point_count),
        ):
            reader = create_incremental_sparse_model_reader(
                discover_sparse_model(model_directory)
            )
            try:
                _advance_to_points_stage(reader)
                progress = reader.step(max_records=1)
                assert progress.stage == STAGE_POINTS3D
                assert progress.current_count == 1
                assert progress.total_count == expected_total
                assert reader.has_open_file
                chunk_count = 1
                while not reader.is_completed:
                    reader.step(max_records=17)
                    chunk_count += 1
                assert chunk_count > 2
                assert reader.model.points.count == point_count
                assert not reader.has_open_file
            finally:
                reader.close()


def test_incremental_reader_cancel_closes_open_points3d_file() -> None:
    """points3D 청크 도중 취소하면 binary·text 파일 핸들이 즉시 닫히는지 확인합니다."""
    from tools.align.colmap_io import (
        STAGE_CANCELLED,
        create_incremental_sparse_model_reader,
        discover_sparse_model,
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        text_directory = root / "cancel_text_sparse"
        binary_directory = root / "cancel_binary_sparse"
        _write_many_points_text_model(text_directory, 8)
        _write_many_points_binary_model(binary_directory, 8)
        for model_directory in (text_directory, binary_directory):
            reader = create_incremental_sparse_model_reader(
                discover_sparse_model(model_directory)
            )
            try:
                _advance_to_points_stage(reader)
                reader.step(max_records=1)
                assert reader.has_open_file
                reader.cancel()
                assert reader.stage == STAGE_CANCELLED
                assert reader.is_cancelled
                assert not reader.has_open_file
                assert reader.step(max_records=1).is_cancelled
            finally:
                reader.close()


def test_incremental_reader_exact_last_record_builds_model() -> None:
    """마지막 points3D 레코드와 정확히 같은 예산에서도 완료 모델이 즉시 노출되는지 확인합니다."""
    from tools.align.colmap_io import (
        create_incremental_sparse_model_reader,
        discover_sparse_model,
    )

    point_count = 5
    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "exact_boundary_sparse"
        _write_many_points_binary_model(model_directory, point_count)
        reader = create_incremental_sparse_model_reader(discover_sparse_model(model_directory))
        try:
            _advance_to_points_stage(reader)
            progress = reader.step(max_records=point_count)
            assert progress.is_completed
            assert reader.is_completed
            assert reader.model.points.count == point_count
            assert not reader.has_open_file
        finally:
            reader.close()


def test_incremental_reader_rejects_non_progressing_budgets() -> None:
    """영·비정수·비유한 청크 예산이 reader를 무진행 상태로 만들지 않는지 확인합니다."""
    from tools.align.colmap_io import (
        create_incremental_sparse_model_reader,
        discover_sparse_model,
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "budget_sparse"
        _write_text_model(model_directory)
        reader = create_incremental_sparse_model_reader(discover_sparse_model(model_directory))
        try:
            _expect_error(ValueError, lambda: reader.step(max_records=0))
            _expect_error(ValueError, lambda: reader.step(max_records=1.5))
            _expect_error(ValueError, lambda: reader.step(time_budget_seconds=0.0))
            _expect_error(ValueError, lambda: reader.step(time_budget_seconds=float("nan")))
            _expect_error(ValueError, lambda: reader.step(time_budget_seconds=float("inf")))
            assert not reader.has_open_file
        finally:
            reader.close()


def test_text_reader_accepts_realistic_large_points2d_line() -> None:
    """1 MiB를 넘는 images.txt points2D 줄도 명시 상한 안에서 읽는지 확인합니다."""
    from tools.align.colmap_io import discover_sparse_model, read_sparse_model

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory) / "large_points2d_sparse"
        _write_large_points2d_line_text_model(model_directory, 160_000)
        model = read_sparse_model(discover_sparse_model(model_directory))
        assert model.images.count == 1
        assert model.images.point2d_xy.shape == (160_000, 2)
        assert model.images.point2d_point3d_ids[0] == 1


def run_all() -> None:
    """COLMAP I/O 검사를 Blender 한 프로세스에서 순서대로 실행합니다."""
    _add_source_path()
    from tools.align.logging_utils import configure_logging, shutdown_logging

    configure_logging("INFO")
    try:
        test_text_and_binary_sparse_models_have_same_contract()
        test_camera_model_table_covers_supported_colmap_ids()
        test_discovery_matches_reference_format_selection()
        test_discovery_prioritizes_standard_sparse_zero_and_rejects_nonstandard_ambiguity()
        test_discovery_infers_dataset_root_from_standard_subfolders()
        test_validation_rejects_broken_bidirectional_track_reference()
        test_binary_parser_rejects_truncation_and_trailing_bytes()
        test_text_parser_rejects_unknown_camera_model()
        test_text_readers_preserve_image_names_with_spaces()
        test_incremental_reader_handles_binary_and_text_in_multiple_chunks()
        test_incremental_reader_cancel_closes_open_points3d_file()
        test_incremental_reader_exact_last_record_builds_model()
        test_incremental_reader_rejects_non_progressing_budgets()
        test_text_reader_accepts_realistic_large_points2d_line()
    finally:
        shutdown_logging()
    print("test_colmap_io: 통과")


if __name__ == "__main__":
    run_all()
