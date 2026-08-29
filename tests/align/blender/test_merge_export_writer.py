"""COLMAP modern 5파일 writer의 byte 계약과 reader 왕복을 Blender에서 검사합니다."""

from __future__ import annotations

from dataclasses import replace
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


def _model() -> object:
    """writer golden fixture에 쓸 작은 modern COLMAP 모델을 만듭니다."""

    from tools.align.colmap_io.types import (
        CameraRecord,
        ColmapModelData,
        FrameDataRecord,
        FrameRecord,
        ImageRecord,
        ImageTable,
        INVALID_POINT3D_ID,
        PointTable,
        RigRecord,
        RigSensorRecord,
    )

    camera = CameraRecord(
        camera_id=1,
        model_id=1,
        model_name="PINHOLE",
        width=640,
        height=480,
        params_float64=np.asarray((500.0, 510.0, 320.0, 240.0)),
    )
    image_two = ImageRecord(
        image_id=2,
        camera_id=1,
        name="a/image2.jpg",
        cam_from_world=np.asarray((1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0)),
    )
    image_one = ImageRecord(
        image_id=1,
        camera_id=1,
        name="b/image1.jpg",
        cam_from_world=np.asarray((1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    )
    reference_sensor = RigSensorRecord(sensor_type="CAMERA", sensor_id=1)
    rig = RigRecord(
        rig_id=5,
        reference_sensor=reference_sensor,
        sensors=(reference_sensor,),
    )
    frame_two = FrameRecord(
        frame_id=8,
        rig_id=5,
        rig_from_world=image_two.cam_from_world,
        data_ids=(FrameDataRecord(sensor_type="CAMERA", sensor_id=1, data_id=2),),
    )
    frame_one = FrameRecord(
        frame_id=4,
        rig_id=5,
        rig_from_world=image_one.cam_from_world,
        data_ids=(FrameDataRecord(sensor_type="CAMERA", sensor_id=1, data_id=1),),
    )
    return ColmapModelData(
        model_id="writer-fixture",
        display_name="writer-fixture",
        source_path=Path("writer-fixture"),
        source_format="binary",
        cameras={1: camera},
        images=ImageTable(
            image_records={2: image_two, 1: image_one},
            point2d_offsets=np.asarray((0, 2, 3), dtype=np.uint64),
            point2d_xy=np.asarray(
                (
                    (10.0, 20.0),
                    (30.0, 40.0),
                    (11.0, 21.0),
                ),
                dtype=np.float64,
            ),
            point2d_point3d_ids=np.asarray(
                (7, INVALID_POINT3D_ID, 7),
                dtype=np.uint64,
            ),
        ),
        rigs={5: rig},
        frames={8: frame_two, 4: frame_one},
        points=PointTable(
            point_ids=np.asarray((7,), dtype=np.uint64),
            positions=np.asarray(((1.0, 2.0, 3.0),), dtype=np.float64),
            colors=np.asarray(((10, 20, 30),), dtype=np.uint8),
            errors=np.asarray((0.5,), dtype=np.float64),
            track_offsets=np.asarray((0, 2), dtype=np.uint64),
            track_image_ids=np.asarray((1, 2), dtype=np.uint64),
            track_point2d_indices=np.asarray((0, 0), dtype=np.uint32),
            display_indices=np.asarray((0,), dtype=np.uint32),
        ),
        bounds_min=np.asarray((1.0, 2.0, 3.0), dtype=np.float64),
        bounds_max=np.asarray((1.0, 2.0, 3.0), dtype=np.float64),
    )


def _golden_files() -> dict[str, bytes]:
    """ID 오름차순과 little-endian 고정을 함께 확인할 5종 golden bytes를 만듭니다."""

    invalid_point_id = np.iinfo(np.uint64).max
    cameras = struct.pack("<QIiQQ4d", 1, 1, 1, 640, 480, 500.0, 510.0, 320.0, 240.0)

    rigs = struct.pack("<QIIiI", 1, 5, 1, 0, 1)

    frames = struct.pack("<Q", 2)
    frames += struct.pack("<II7dI", 4, 5, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1)
    frames += struct.pack("<iIQ", 0, 1, 1)
    frames += struct.pack("<II7dI", 8, 5, 1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 1)
    frames += struct.pack("<iIQ", 0, 1, 2)

    images = struct.pack("<Q", 2)
    images += struct.pack("<I7dI", 1, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1)
    images += b"b/image1.jpg\0"
    images += struct.pack("<QddQ", 1, 11.0, 21.0, 7)
    images += struct.pack("<I7dI", 2, 1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 1)
    images += b"a/image2.jpg\0"
    images += struct.pack("<QddQddQ", 2, 10.0, 20.0, 7, 30.0, 40.0, invalid_point_id)

    points = struct.pack("<Q", 1)
    points += struct.pack("<Q3d3BdQ", 7, 1.0, 2.0, 3.0, 10, 20, 30, 0.5, 2)
    points += struct.pack("<IIII", 1, 0, 2, 0)
    return {
        "rigs.bin": rigs,
        "cameras.bin": cameras,
        "frames.bin": frames,
        "images.bin": images,
        "points3D.bin": points,
    }


def _golden_text_files() -> dict[str, str]:
    """공식 5파일 레이아웃과 결정적 ID 정렬을 확인할 golden text를 반환합니다."""

    return {
        "rigs.txt": (
            "# Rig calib list with one line of data per calib:\n"
            "#   RIG_ID, NUM_SENSORS, REF_SENSOR_TYPE, REF_SENSOR_ID, SENSORS[] as (SENSOR_TYPE, SENSOR_ID, HAS_POSE, [QW, QX, QY, QZ, TX, TY, TZ])\n"
            "# Number of rigs: 1\n"
            "5 1 CAMERA 1\n"
        ),
        "cameras.txt": (
            "# Camera list with one line of data per camera:\n"
            "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
            "# Number of cameras: 1\n"
            "1 PINHOLE 640 480 500 510 320 240\n"
        ),
        "frames.txt": (
            "# Frame list with one line of data per frame:\n"
            "#   FRAME_ID, RIG_ID, RIG_FROM_WORLD[QW, QX, QY, QZ, TX, TY, TZ], NUM_DATA_IDS, DATA_IDS[] as (SENSOR_TYPE, SENSOR_ID, DATA_ID)\n"
            "# Number of frames: 2\n"
            "4 5 1 0 0 0 0 0 0 1 CAMERA 1 1\n"
            "8 5 1 0 0 0 -1 0 0 1 CAMERA 1 2\n"
        ),
        "images.txt": (
            "# Image list with two lines of data per image:\n"
            "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n"
            "#   POINTS2D[] as (X, Y, POINT3D_ID)\n"
            "# Number of images: 2, mean observations per image: 1.5\n"
            "1 1 0 0 0 0 0 0 1 b/image1.jpg\n"
            "11 21 7\n"
            "2 1 0 0 0 -1 0 0 1 a/image2.jpg\n"
            "10 20 7 30 40 -1\n"
        ),
        "points3D.txt": (
            "# 3D point list with one line of data per point:\n"
            "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
            "# Number of points: 1, mean track length: 2\n"
            "7 1 2 3 10 20 30 0.5 1 0 2 0\n"
        ),
    }


def _expect_error(error_type: type[Exception], callback: object) -> None:
    """지정한 예외가 없으면 writer 경계 검사가 실패한 것으로 처리합니다."""

    try:
        callback()  # type: ignore[operator]
    except error_type:
        return
    raise AssertionError(f"{error_type.__name__} 예외가 발생해야 합니다.")


def test_writer_matches_golden_bytes_and_reader_roundtrip() -> None:
    """5개 파일의 exact bytes, invalid sentinel, reader 왕복 구조를 함께 확인합니다."""

    from tools.align.colmap_io import load_sparse_model
    from tools.align.colmap_io.binary_writer import write_binary_model
    from tools.align.colmap_io.types import INVALID_POINT3D_ID

    with tempfile.TemporaryDirectory() as temporary_directory:
        output_directory = Path(temporary_directory) / "sparse" / "0"
        written_paths = write_binary_model(_model(), output_directory)
        golden_files = _golden_files()
        assert tuple(written_paths) == (
            "rigs.bin",
            "cameras.bin",
            "frames.bin",
            "images.bin",
            "points3D.bin",
        )
        for filename, expected_bytes in golden_files.items():
            actual_path = output_directory / filename
            assert written_paths[filename] == actual_path
            assert actual_path.read_bytes() == expected_bytes

        images_bytes = (output_directory / "images.bin").read_bytes()
        assert b"a/image2.jpg\0" in images_bytes
        assert struct.pack("<Q", INVALID_POINT3D_ID) in images_bytes
        assert struct.unpack("<Q", (output_directory / "cameras.bin").read_bytes()[:8])[0] == 1

        loaded = load_sparse_model(output_directory)
        assert tuple(loaded.rigs) == (5,)
        assert tuple(loaded.cameras) == (1,)
        assert tuple(loaded.frames) == (4, 8)
        assert tuple(loaded.images.image_records) == (1, 2)
        assert np.array_equal(loaded.points.point_ids, np.asarray((7,), dtype=np.uint64))
        assert np.array_equal(
            loaded.images.point2d_point3d_ids,
            np.asarray((7, 7, INVALID_POINT3D_ID), dtype=np.uint64),
        )
        assert np.array_equal(
            loaded.points.track_point2d_indices,
            np.asarray((0, 0), dtype=np.uint32),
        )


def test_text_writer_matches_golden_text_and_reader_roundtrip() -> None:
    """TXT 5파일의 정확한 내용과 invalid sentinel·reader 왕복을 확인합니다."""

    from tools.align.colmap_io import load_sparse_model
    from tools.align.colmap_io.text_writer import write_text_model
    from tools.align.colmap_io.types import INVALID_POINT3D_ID

    with tempfile.TemporaryDirectory() as temporary_directory:
        output_directory = Path(temporary_directory) / "sparse" / "0"
        written_paths = write_text_model(_model(), output_directory)
        golden_files = _golden_text_files()
        assert tuple(written_paths) == (
            "rigs.txt",
            "cameras.txt",
            "frames.txt",
            "images.txt",
            "points3D.txt",
        )
        for filename, expected_text in golden_files.items():
            actual_path = output_directory / filename
            assert written_paths[filename] == actual_path
            assert actual_path.read_text(encoding="utf-8") == expected_text

        loaded = load_sparse_model(output_directory, input_format="TEXT")
        assert tuple(loaded.rigs) == (5,)
        assert tuple(loaded.cameras) == (1,)
        assert tuple(loaded.frames) == (4, 8)
        assert tuple(loaded.images.image_records) == (1, 2)
        assert np.array_equal(
            loaded.images.point2d_point3d_ids,
            np.asarray((7, 7, INVALID_POINT3D_ID), dtype=np.uint64),
        )
        assert np.array_equal(
            loaded.points.track_point2d_indices,
            np.asarray((0, 0), dtype=np.uint32),
        )


def test_text_writer_accepts_spaced_names_and_rejects_invalid_inputs() -> None:
    """공백 포함 이름은 왕복하고 미정규화 모델·줄바꿈 이름은 거부하는지 확인합니다."""

    from tools.align.colmap_io.text_writer import ColmapTextWriteError, write_text_model
    from tools.align.colmap_io.types import ImageRecord, ImageTable

    model = _model()
    with tempfile.TemporaryDirectory() as temporary_directory:
        _expect_error(
            ColmapTextWriteError,
            lambda: write_text_model(
                replace(model, rigs={}, frames={}),
                Path(temporary_directory) / "legacy",
            ),
        )
        original = model.images.image_records[1]
        expected_name = "반가사유상 촬영 (28).jpg"
        spaced_images = ImageTable(
            image_records={
                **model.images.image_records,
                1: ImageRecord(
                    image_id=original.image_id,
                    camera_id=original.camera_id,
                    name=expected_name,
                    cam_from_world=original.cam_from_world,
                ),
            },
            point2d_offsets=model.images.point2d_offsets,
            point2d_xy=model.images.point2d_xy,
            point2d_point3d_ids=model.images.point2d_point3d_ids,
        )
        spaced_directory = Path(temporary_directory) / "spaced-name"
        write_text_model(replace(model, images=spaced_images), spaced_directory)
        images_text = (spaced_directory / "images.txt").read_text(encoding="utf-8")
        assert f" 1 {expected_name}\n" in images_text

        from tools.align.colmap_io import load_sparse_model

        loaded = load_sparse_model(spaced_directory, input_format="TEXT")
        assert loaded.images.image_records[1].name == expected_name

        line_break_images = ImageTable(
            image_records={
                **model.images.image_records,
                1: ImageRecord(
                    image_id=original.image_id,
                    camera_id=original.camera_id,
                    name="bad\nimage.jpg",
                    cam_from_world=original.cam_from_world,
                ),
            },
            point2d_offsets=model.images.point2d_offsets,
            point2d_xy=model.images.point2d_xy,
            point2d_point3d_ids=model.images.point2d_point3d_ids,
        )
        _expect_error(
            ColmapTextWriteError,
            lambda: write_text_model(
                replace(model, images=line_break_images),
                Path(temporary_directory) / "line-break-name",
            ),
        )


def test_writer_rejects_legacy_and_invalid_binary_only_fields() -> None:
    """legacy 미정규화, NUL 이름, uint32 밖 track index를 조용히 기록하지 않는지 확인합니다."""

    from tools.align.colmap_io.binary_writer import (
        ColmapWriteError,
        write_binary_model,
        write_images_binary,
        write_points3d_binary,
    )
    from tools.align.colmap_io.types import ImageRecord, ImageTable, PointTable

    model = _model()
    legacy_model = replace(model, rigs={}, frames={})
    with tempfile.TemporaryDirectory() as temporary_directory:
        output_directory = Path(temporary_directory) / "legacy"
        _expect_error(
            ColmapWriteError,
            lambda: write_binary_model(legacy_model, output_directory),
        )
        assert not output_directory.exists()

        invalid_name_images = ImageTable(
            image_records={
                1: ImageRecord(
                    image_id=1,
                    camera_id=1,
                    name="bad\0name.jpg",
                    cam_from_world=np.asarray((1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
                )
            },
            point2d_offsets=np.asarray((0, 0), dtype=np.uint64),
            point2d_xy=np.empty((0, 2), dtype=np.float64),
            point2d_point3d_ids=np.empty((0,), dtype=np.uint64),
        )
        invalid_name_path = Path(temporary_directory) / "invalid-name.bin"
        _expect_error(
            ColmapWriteError,
            lambda: write_images_binary(invalid_name_images, invalid_name_path),
        )
        assert not invalid_name_path.exists()

        out_of_range_tracks = PointTable(
            point_ids=np.asarray((1,), dtype=np.uint64),
            positions=np.asarray(((0.0, 0.0, 1.0),), dtype=np.float64),
            colors=np.asarray(((0, 0, 0),), dtype=np.uint8),
            errors=np.asarray((0.0,), dtype=np.float64),
            track_offsets=np.asarray((0, 1), dtype=np.uint64),
            track_image_ids=np.asarray((1,), dtype=np.uint64),
            track_point2d_indices=np.asarray((np.iinfo(np.uint32).max + 1,), dtype=np.uint64),
            display_indices=np.asarray((0,), dtype=np.uint32),
        )
        _expect_error(
            ColmapWriteError,
            lambda: write_points3d_binary(
                out_of_range_tracks,
                Path(temporary_directory) / "out-of-range.bin",
            ),
        )


def test_legacy_normalize_write_reader_roundtrip() -> None:
    """legacy synthetic 모델을 trivial modern 구조로 만든 뒤 5파일 왕복을 확인합니다."""

    from tools.align.alignment.types import identity_sim3_transform
    from tools.align.colmap_io import load_sparse_model
    from tools.align.colmap_io.binary_writer import write_binary_model
    from tools.align.merge_export.transforms import normalize_legacy_rigs_and_frames

    legacy_model = replace(_model(), rigs={}, frames={})
    rigs, frames = normalize_legacy_rigs_and_frames(
        legacy_model,
        identity_sim3_transform(),
    )
    modern_model = replace(legacy_model, rigs=rigs, frames=frames)
    with tempfile.TemporaryDirectory() as temporary_directory:
        output_directory = Path(temporary_directory) / "legacy-modern"
        write_binary_model(modern_model, output_directory)
        loaded = load_sparse_model(output_directory)
        assert tuple(loaded.rigs) == (1,)
        assert tuple(loaded.frames) == (1, 2)
        assert loaded.frames[1].data_ids[0].sensor_id == 1
        assert loaded.frames[2].data_ids[0].data_id == 2
        assert tuple(loaded.images.image_records) == (1, 2)


def run_all() -> None:
    """merge export text·binary writer 검사를 Blender 한 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_writer_matches_golden_bytes_and_reader_roundtrip()
    test_text_writer_matches_golden_text_and_reader_roundtrip()
    test_text_writer_accepts_spaced_names_and_rejects_invalid_inputs()
    test_writer_rejects_legacy_and_invalid_binary_only_fields()
    test_legacy_normalize_write_reader_roundtrip()
    print("test_merge_export_writer: 통과")


if __name__ == "__main__":
    run_all()
