"""구조 통합 엔진과 worker의 작은 modern COLMAP 합성 경로를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import sys
from tempfile import TemporaryDirectory

import numpy as np


def _add_source_path() -> None:
    """애드온 source 디렉터리를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _identity_pose() -> np.ndarray:
    """원점에서 +Z를 보는 identity COLMAP world→camera pose를 반환합니다."""

    return np.asarray((1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), dtype=np.float64)


def _dataset_root_for_model_path(model_path: Path) -> Path:
    """표준 sparse/0 모델 경로 또는 직접 모델 경로에서 테스트 Dataset Root를 반환합니다."""

    resolved = model_path.resolve(strict=False)
    if resolved.name.casefold() == "0" and resolved.parent.name.casefold() == "sparse":
        return resolved.parent.parent
    if resolved.name.casefold() == "sparse":
        return resolved.parent
    return resolved


def _modern_model(
    root: Path,
    *,
    model_id: str,
    image_names: tuple[str, str] = ("a.jpg", "b.jpg"),
    second_image_extra_feature: bool = False,
    point_position: tuple[float, float, float] = (0.0, 0.0, 1.0),
):
    """한 점·두 관측·trivial modern rig/frame을 갖는 작은 정상 모델을 만듭니다."""

    from tools.align.colmap_io import (
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
        calculate_bounds,
        validate_model,
    )

    camera = CameraRecord(
        camera_id=1,
        model_id=0,
        model_name="SIMPLE_PINHOLE",
        width=32,
        height=32,
        params_float64=np.asarray((1.0, 0.0, 0.0), dtype=np.float64),
    )
    image_records = {
        1: ImageRecord(1, 1, image_names[0], _identity_pose()),
        2: ImageRecord(2, 1, image_names[1], _identity_pose()),
    }
    second_xy = (
        np.asarray(((0.0, 0.0), (0.5, 0.5)), dtype=np.float64)
        if second_image_extra_feature
        else np.asarray(((0.0, 0.0),), dtype=np.float64)
    )
    point2d_xy = np.ascontiguousarray(
        np.concatenate((np.asarray(((0.0, 0.0),), dtype=np.float64), second_xy), axis=0)
    )
    image_table = ImageTable(
        image_records=image_records,
        point2d_offsets=np.asarray((0, 1, point2d_xy.shape[0]), dtype=np.uint64),
        point2d_xy=point2d_xy,
        point2d_point3d_ids=np.asarray(
            (1, 1, *([INVALID_POINT3D_ID] if second_image_extra_feature else [])),
            dtype=np.uint64,
        ),
    )
    points = PointTable(
        point_ids=np.asarray((1,), dtype=np.uint64),
        positions=np.asarray((point_position,), dtype=np.float64),
        colors=np.asarray(((10, 20, 30),), dtype=np.uint8),
        errors=np.asarray((0.0,), dtype=np.float64),
        track_offsets=np.asarray((0, 2), dtype=np.uint64),
        track_image_ids=np.asarray((1, 2), dtype=np.uint64),
        track_point2d_indices=np.asarray((0, 0), dtype=np.uint32),
        display_indices=np.asarray((0,), dtype=np.uint64),
    )
    sensor = RigSensorRecord(sensor_type="CAMERA", sensor_id=1)
    rig = RigRecord(rig_id=1, reference_sensor=sensor, sensors=(sensor,))
    frames = {
        1: FrameRecord(1, 1, _identity_pose(), (FrameDataRecord("CAMERA", 1, 1),)),
        2: FrameRecord(2, 1, _identity_pose(), (FrameDataRecord("CAMERA", 1, 2),)),
    }
    bounds_min, bounds_max = calculate_bounds(points.positions)
    model = ColmapModelData(
        model_id=model_id,
        display_name=model_id,
        source_path=root,
        source_format="binary",
        cameras={1: camera},
        images=image_table,
        rigs={1: rig},
        frames=frames,
        points=points,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        geometry_revision=1,
    )
    validate_model(model)
    image_root = _dataset_root_for_model_path(root) / "images"
    for image_name in image_names:
        relative = PurePosixPath(str(image_name).replace("\\", "/"))
        image_path = image_root.joinpath(*relative.parts)
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(f"fixture-image:{relative.name.casefold()}".encode("utf-8"))
    return model


def _source_snapshot(
    model,
    *,
    is_anchor: bool,
    load_order: int,
    expected_source_fingerprint: str = "0" * 64,
):
    """worker와 engine 테스트가 공유하는 identity A 그룹 source snapshot을 만듭니다."""

    from tools.align.merge_export.types import MergeSourceModelSnapshot

    return MergeSourceModelSnapshot(
        model_id=model.model_id,
        display_name=model.display_name,
        dataset_root=_dataset_root_for_model_path(model.source_path),
        source_path=model.source_path,
        source_format="binary",
        catalog_load_order=load_order,
        geometry_revision=model.geometry_revision,
        is_anchor=is_anchor,
        display_matrix_float64=np.identity(4, dtype=np.float64),
        display_fingerprint=f"display-{model.model_id}",
        alignment_matrix_float64=np.identity(4, dtype=np.float64),
        alignment_fingerprint=f"alignment-{model.model_id}",
        expected_source_fingerprint=expected_source_fingerprint,
        alignment_provenance={"model_id": model.model_id, "method": "ANCHOR" if is_anchor else "MANUAL"},
    )


def test_engine_keeps_observation_tracks_and_writes_deterministic_id_maps() -> None:
    """engine이 spatial merge 없이 representative point·양방향 track·NPZ-safe map을 만드는지 확인합니다."""

    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource

    with TemporaryDirectory(prefix="colmap_align_merge_engine_") as directory:
        root = Path(directory)
        model = _modern_model(root / "source", model_id="anchor")
        result = merge_baked_models(
            (LoadedMergeSource(0, _source_snapshot(model, is_anchor=True, load_order=0), model),),
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_test",
        )
        assert result.model.points.count == 1
        assert result.model.images.count == 2
        assert result.model.points.track_image_ids.tolist() == [1, 2]
        assert result.model.images.point2d_point3d_ids.tolist() == [1, 1]
        assert result.id_maps.point3d_ids[(0, 1)] == 1
        arrays = result.id_maps.to_npz_arrays()
        assert all(array.dtype != np.dtype(object) for array in arrays.values())
        assert result.duplicate_image_group_count == 0
        assert result.merged_point_component_count == 0


def test_engine_skips_reprojection_when_basenames_do_not_overlap() -> None:
    """고유 basename에서도 source Point3D와 기존 error를 그대로 보존합니다."""

    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource
    from tools.align.merge_export.types import MergePolicy

    with TemporaryDirectory(prefix="colmap_align_merge_fast_path_") as directory:
        root = Path(directory)
        model = _modern_model(root / "source", model_id="anchor")
        result = merge_baked_models(
            (LoadedMergeSource(0, _source_snapshot(model, is_anchor=True, load_order=0), model),),
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_fast_path",
        )
        assert result.model.points.count == model.points.count
        assert result.model.points.errors.tolist() == model.points.errors.tolist()


def test_engine_appends_different_point2d_counts_with_source_offsets() -> None:
    """동일 basename의 서로 다른 points2D 배열을 source별 offset 구간으로 보존합니다."""

    from tools.align.colmap_io import INVALID_POINT3D_ID
    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource, run_preflight
    from tools.align.merge_export.types import MergeExportSnapshot

    with TemporaryDirectory(prefix="colmap_align_merge_preflight_") as directory:
        root = Path(directory)
        anchor = _modern_model(root / "anchor", model_id="anchor")
        other = _modern_model(
            root / "other",
            model_id="other",
            image_names=("different/a.jpg", "different/b.jpg"),
            second_image_extra_feature=True,
        )
        snapshot = MergeExportSnapshot(
            job_id="point2d-mismatch",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            anchor_model_id="anchor",
            source_models=(
                _source_snapshot(anchor, is_anchor=True, load_order=0),
                _source_snapshot(other, is_anchor=False, load_order=1),
            ),
            runtime_fingerprint="runtime-fingerprint",
        )
        sources = (
            LoadedMergeSource(0, snapshot.source_models[0], anchor),
            LoadedMergeSource(1, snapshot.source_models[1], other),
        )
        preflight = run_preflight(snapshot, loaded_sources=sources)
        assert preflight.is_ready
        result = merge_baked_models(
            sources,
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_point2d_offsets",
        )
        assert result.model.images.point2d_offsets.tolist() == [0, 2, 5]
        assert result.model.points.track_point2d_indices.tolist() == [0, 0, 1, 1]
        assert result.model.images.point2d_point3d_ids.tolist() == [
            1,
            2,
            1,
            2,
            INVALID_POINT3D_ID,
        ]


def test_preflight_warns_for_reencoded_duplicate_and_keeps_anchor_representative() -> None:
    """같은 촬영 이미지의 재인코딩 차이를 허용하고 Anchor 파일을 대표로 유지합니다."""

    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource, run_preflight
    from tools.align.merge_export.types import MergeExportSnapshot
    from tools.align.merge_export.worker import _build_image_copy_plan

    with TemporaryDirectory(prefix="colmap_align_merge_image_bytes_") as directory:
        root = Path(directory)
        anchor = _modern_model(root / "anchor", model_id="anchor")
        other = _modern_model(root / "other", model_id="other")
        (_dataset_root_for_model_path(other.source_path) / "images" / "a.jpg").write_bytes(
            b"different-image-bytes"
        )
        snapshot = MergeExportSnapshot(
            job_id="image-bytes-mismatch",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            anchor_model_id="anchor",
            source_models=(
                _source_snapshot(anchor, is_anchor=True, load_order=0),
                _source_snapshot(other, is_anchor=False, load_order=1),
            ),
            runtime_fingerprint="runtime-fingerprint",
        )
        sources = (
            LoadedMergeSource(0, snapshot.source_models[0], anchor),
            LoadedMergeSource(1, snapshot.source_models[1], other),
        )
        result = run_preflight(snapshot, loaded_sources=sources)
        assert result.is_ready
        assert any(
            issue.code == "DUPLICATE_IMAGE_FILE_REENCODED"
            for issue in result.warnings
        )
        assert not any(
            issue.code == "DUPLICATE_IMAGE_FILE_REENCODED" for issue in result.errors
        )

        engine_result = merge_baked_models(
            sources,
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_reencoded",
        )
        copy_plan = _build_image_copy_plan(root / "candidate", sources, engine_result)
        anchor_image_root = _dataset_root_for_model_path(anchor.source_path) / "images"
        assert all(entry.source_path.parent == anchor_image_root for entry in copy_plan)


def test_preflight_warns_and_uses_anchor_for_independent_camera_and_pose() -> None:
    """동일 해상도의 독립 calibration·pose 차이를 경고하고 Anchor 대표를 유지합니다."""

    from dataclasses import replace

    from tools.align.colmap_io import validate_model
    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource, run_preflight
    from tools.align.merge_export.types import MergeExportSnapshot

    with TemporaryDirectory(prefix="colmap_align_merge_camera_pose_representative_") as directory:
        root = Path(directory)
        anchor = _modern_model(root / "anchor", model_id="anchor")
        other = _modern_model(root / "other", model_id="other")
        other = replace(
            other,
            cameras={
                1: replace(
                    other.cameras[1],
                    params_float64=np.asarray((2.0, 1.0, 1.0), dtype=np.float64),
                )
            },
        )
        validate_model(other)
        other_alignment = np.identity(4, dtype=np.float64)
        other_alignment[0, 3] = 1.0
        anchor_snapshot = _source_snapshot(anchor, is_anchor=True, load_order=0)
        other_snapshot = replace(
            _source_snapshot(other, is_anchor=False, load_order=1),
            alignment_matrix_float64=other_alignment,
            alignment_fingerprint="alignment-other-shifted",
        )
        snapshot = MergeExportSnapshot(
            job_id="camera-pose-representative",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            anchor_model_id="anchor",
            source_models=(anchor_snapshot, other_snapshot),
            runtime_fingerprint="runtime-fingerprint",
        )
        sources = (
            LoadedMergeSource(0, anchor_snapshot, anchor),
            LoadedMergeSource(1, other_snapshot, other),
        )
        preflight = run_preflight(snapshot, loaded_sources=sources)
        warning_codes = {issue.code for issue in preflight.warnings}
        assert preflight.is_ready
        assert "DUPLICATE_IMAGE_CAMERA_REPRESENTATIVE" in warning_codes
        assert "DUPLICATE_IMAGE_POSE_REPRESENTATIVE" in warning_codes

        result = merge_baked_models(
            sources,
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_camera_pose_representative",
        )
        output_image = result.model.images.image_records[1]
        assert output_image.camera_id == result.id_maps.camera_ids[(0, 1)]
        assert np.array_equal(output_image.cam_from_world, anchor.images.image_records[1].cam_from_world)


def test_preflight_still_rejects_duplicate_with_different_resolution() -> None:
    """동일 basename이라도 COLMAP 이미지 크기가 다르면 대표 통합을 차단합니다."""

    from dataclasses import replace

    from tools.align.colmap_io import validate_model
    from tools.align.merge_export.preflight import LoadedMergeSource, run_preflight
    from tools.align.merge_export.types import MergeExportSnapshot

    with TemporaryDirectory(prefix="colmap_align_merge_resolution_mismatch_") as directory:
        root = Path(directory)
        anchor = _modern_model(root / "anchor", model_id="anchor")
        other = _modern_model(root / "other", model_id="other")
        other = replace(
            other,
            cameras={1: replace(other.cameras[1], width=64)},
        )
        validate_model(other)
        snapshot = MergeExportSnapshot(
            job_id="resolution-mismatch",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            anchor_model_id="anchor",
            source_models=(
                _source_snapshot(anchor, is_anchor=True, load_order=0),
                _source_snapshot(other, is_anchor=False, load_order=1),
            ),
            runtime_fingerprint="runtime-fingerprint",
        )
        result = run_preflight(
            snapshot,
            loaded_sources=(
                LoadedMergeSource(0, snapshot.source_models[0], anchor),
                LoadedMergeSource(1, snapshot.source_models[1], other),
            ),
        )
        assert not result.is_ready
        assert any(
            issue.code == "DUPLICATE_IMAGE_RESOLUTION_MISMATCH"
            for issue in result.errors
        )


def test_engine_offsets_shared_image_features_without_merging_point3d() -> None:
    """같은 basename의 source 특징과 Point3D를 offset·전역 ID로 각각 보존합니다."""

    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource, run_preflight
    from tools.align.merge_export.types import MergeExportSnapshot
    from tools.align.merge_export.worker import _unreferenced_entity_summary

    with TemporaryDirectory(prefix="colmap_align_merge_two_sources_") as directory:
        root = Path(directory)
        anchor = _modern_model(root / "anchor", model_id="anchor")
        other = _modern_model(
            root / "other",
            model_id="other",
            point_position=(0.0, 0.0, 2.0),
        )
        snapshot = MergeExportSnapshot(
            job_id="shared-observation",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            anchor_model_id="anchor",
            source_models=(
                _source_snapshot(anchor, is_anchor=True, load_order=0),
                _source_snapshot(other, is_anchor=False, load_order=1),
            ),
            runtime_fingerprint="runtime-fingerprint",
        )
        sources = (
            LoadedMergeSource(0, snapshot.source_models[0], anchor),
            LoadedMergeSource(1, snapshot.source_models[1], other),
        )
        preflight = run_preflight(snapshot, loaded_sources=sources)
        assert preflight.is_ready
        result = merge_baked_models(
            sources,
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_shared_observation",
        )

        # 두 모델의 이미지는 대표화하지만 points2D는 source별 구간으로 이어 붙입니다.
        assert result.model.images.count == 2
        assert result.model.images.point2d_offsets.tolist() == [0, 2, 4]
        assert result.model.points.count == 2
        assert result.model.points.track_image_ids.tolist() == [1, 2, 1, 2]
        assert result.model.points.track_point2d_indices.tolist() == [0, 0, 1, 1]
        assert result.model.images.point2d_point3d_ids.tolist() == [1, 2, 1, 2]
        assert np.array_equal(result.model.points.positions[0], anchor.points.positions[0])
        assert np.array_equal(result.model.points.positions[1], other.points.positions[0])
        assert result.duplicate_image_group_count == 2
        assert result.merged_point_component_count == 0

        # Anchor가 이미지 대표이고, source별 Point3D는 충돌 없는 전역 ID로 남습니다.
        assert result.id_maps.point3d_ids == {(0, 1): 1, (1, 1): 2}
        assert result.id_maps.rig_ids == {(0, 1): 1, (1, 1): 2}
        assert result.id_maps.camera_ids == {(0, 1): 1, (1, 1): 2}
        assert result.id_maps.frame_ids == {(0, 1): 1, (0, 2): 2, (1, 1): 1, (1, 2): 2}
        assert result.id_maps.image_ids == {(0, 1): 1, (0, 2): 2, (1, 1): 1, (1, 2): 2}
        assert _unreferenced_entity_summary(result.model) == {
            "unreferenced_cameras": 1,
            "unreferenced_rigs": 1,
        }


def test_engine_applies_cumulative_offsets_for_three_sources() -> None:
    """세 source의 같은 이미지 특징 구간이 0·1·2 offset으로 누적되는지 검증합니다."""

    from tools.align.merge_export.merge_engine import merge_baked_models
    from tools.align.merge_export.preflight import LoadedMergeSource

    with TemporaryDirectory(prefix="colmap_align_merge_three_offsets_") as directory:
        root = Path(directory)
        models = tuple(
            _modern_model(
                root / f"source-{ordinal}",
                model_id=f"model-{ordinal}",
                point_position=(0.0, 0.0, float(ordinal + 1)),
            )
            for ordinal in range(3)
        )
        sources = tuple(
            LoadedMergeSource(
                ordinal,
                _source_snapshot(
                    model,
                    is_anchor=ordinal == 0,
                    load_order=ordinal,
                ),
                model,
            )
            for ordinal, model in enumerate(models)
        )
        result = merge_baked_models(
            sources,
            output_source_path=root / "candidate" / "sparse" / "0",
            output_model_id="merged_three_offsets",
        )
        assert result.model.images.point2d_offsets.tolist() == [0, 3, 6]
        assert result.model.points.track_point2d_indices.tolist() == [
            0,
            0,
            1,
            1,
            2,
            2,
        ]
        assert result.id_maps.point3d_ids == {
            (0, 1): 1,
            (1, 1): 2,
            (2, 1): 3,
        }


def test_preflight_explains_inconsistent_modern_rig_frame_pose() -> None:
    """modern image pose가 rig/frame 합성과 다르면 bake 전에 원인·영향·해결을 반환합니다."""

    from dataclasses import replace

    from tools.align.colmap_io import ImageRecord, ImageTable, validate_model
    from tools.align.merge_export.preflight import LoadedMergeSource, run_preflight
    from tools.align.merge_export.types import MergeExportSnapshot

    with TemporaryDirectory(prefix="colmap_align_merge_pose_preflight_") as directory:
        root = Path(directory)
        model = _modern_model(root / "source", model_id="anchor")
        records = dict(model.images.image_records)
        original = records[1]
        records[1] = ImageRecord(
            image_id=original.image_id,
            camera_id=original.camera_id,
            name=original.name,
            cam_from_world=np.asarray((1.0, 0.0, 0.0, 0.0, 0.25, 0.0, 0.0), dtype=np.float64),
        )
        mismatched_images = ImageTable(
            image_records=records,
            point2d_offsets=model.images.point2d_offsets,
            point2d_xy=model.images.point2d_xy,
            point2d_point3d_ids=model.images.point2d_point3d_ids,
        )
        mismatched_model = replace(model, images=mismatched_images)
        validate_model(mismatched_model)
        snapshot = MergeExportSnapshot(
            job_id="modern-pose-mismatch",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            anchor_model_id="anchor",
            source_models=(_source_snapshot(mismatched_model, is_anchor=True, load_order=0),),
            runtime_fingerprint="runtime-fingerprint",
        )
        result = run_preflight(
            snapshot,
            loaded_sources=(LoadedMergeSource(0, snapshot.source_models[0], mismatched_model),),
        )
        issue = next(
            item
            for item in result.errors
            if item.code == "MODERN_RIG_FRAME_IMAGE_POSE_MISMATCH"
        )
        assert "rig/frame pose" in issue.cause
        assert "Project World bake" in issue.impact
        assert "sensor_from_rig" in issue.remedy


def _write_worker_manifest(job_directory: Path, snapshot, *, preflight_only: bool) -> None:
    """backend wrapper와 같은 최소 manifest를 worker 합성 테스트에 기록합니다."""

    job_directory.mkdir(parents=True)
    (job_directory / "manifest.json").write_text(
        json.dumps(
            {"snapshot": snapshot.to_mapping(), "preflight_only": preflight_only},
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_worker_writes_candidate_and_waits_for_main_atomic_finalize() -> None:
    """TXT·Binary candidate가 5파일·audit·roundtrip 뒤 finalize를 기다리는지 확인합니다."""

    from tools.align.colmap_io.binary_writer import write_binary_model
    from tools.align.colmap_io import discover_sparse_model, load_sparse_model, source_fingerprint
    from tools.align.merge_export.types import MergeExportSnapshot, MergeOutputFormat, MergePhase
    from tools.align.merge_export.worker import run_worker_job

    with TemporaryDirectory(prefix="colmap_align_merge_worker_") as directory:
        root = Path(directory)
        source_root = root / "source"
        source_model = _modern_model(
            source_root / "sparse" / "0",
            model_id="anchor",
            point_position=(1.0, 0.0, 1.0),
        )
        write_binary_model(source_model, source_root / "sparse" / "0")
        expected_source_fingerprint = source_fingerprint(
            discover_sparse_model(source_root, input_format="BINARY")
        )
        from dataclasses import replace

        anchor_display = np.identity(4, dtype=np.float64)
        anchor_display[:3, :3] = np.asarray(
            ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )

        cases = (
            (True, MergeOutputFormat.TEXT, ("rigs.txt", "cameras.txt", "frames.txt", "images.txt", "points3D.txt")),
            (False, MergeOutputFormat.BINARY, ("rigs.bin", "cameras.bin", "frames.bin", "images.bin", "points3D.bin")),
        )
        for restore_import_axes, output_format, expected_names in cases:
            suffix = f"{output_format.value.lower()}-{'source' if restore_import_axes else 'project'}"
            output_root = root / f"output-{suffix}"
            candidate_root = root / f".colmap_align_export_worker-{suffix}"
            snapshot = MergeExportSnapshot(
                job_id=f"worker-{suffix}",
                protocol_version="merge-test-v1",
                algorithm_version="merge-test-v1",
                output_root=output_root,
                candidate_root=candidate_root,
                anchor_model_id="anchor",
                source_models=(
                    _source_snapshot(
                        source_model,
                        is_anchor=True,
                        load_order=0,
                        expected_source_fingerprint=expected_source_fingerprint,
                    ),
                ),
                runtime_fingerprint="runtime-fingerprint",
                output_format=output_format,
                restore_import_axes=restore_import_axes,
            )
            # snapshot의 입력 경로는 sparse/0가 아니라 importer가 탐색할 dataset root여야 합니다.
            source = snapshot.source_models[0]
            snapshot = replace(
                snapshot,
                source_models=(
                    replace(
                        source,
                        source_path=source_root,
                        display_matrix_float64=anchor_display,
                        display_fingerprint="display-anchor-z90",
                    ),
                ),
            )
            job_directory = root / f"job-{suffix}"
            _write_worker_manifest(job_directory, snapshot, preflight_only=False)
            result = run_worker_job(job_directory)
            assert result.phase is MergePhase.WAITING_FOR_FINALIZE
            assert result.candidate_root == candidate_root
            assert not output_root.exists()
            for name in expected_names:
                assert (candidate_root / "sparse" / "0" / name).is_file()
            assert (candidate_root / "merge_report.json").is_file()
            assert (candidate_root / "merge_id_map.npz").is_file()
            assert (candidate_root / "images" / "a.jpg").read_bytes() == b"fixture-image:a.jpg"
            assert (candidate_root / "images" / "b.jpg").read_bytes() == b"fixture-image:b.jpg"
            report = json.loads((candidate_root / "merge_report.json").read_text(encoding="utf-8"))
            assert report["schema_version"] == 2
            assert report["output_contract"].startswith("modern COLMAP dataset with images/")
            expected_basis = (
                "anchor_original_colmap_axes" if restore_import_axes else "project_world"
            )
            expected_output_matrix = (
                np.identity(4, dtype=np.float64)
                if restore_import_axes
                else anchor_display
            )
            assert report["restore_import_axes"] is restore_import_axes
            assert report["output_coordinate_basis"] == expected_basis
            assert np.allclose(
                report["source_models"][0]["baked_output_matrix"],
                expected_output_matrix,
                rtol=0.0,
                atol=1.0e-12,
            )
            assert report["sparse_model_format"] == output_format.value
            assert report["point2d_merge_policy"] == "source_order_offset"
            assert report["point3d_merge_policy"] == "global_id_without_cross_source_deduplication"
            assert report["image_copy_summary"]["image_files"] == 2
            source_report = report["source_models"][0]
            assert source_report["expected_source_fingerprint"] == expected_source_fingerprint
            assert source_report["actual_source_fingerprint"] == expected_source_fingerprint
            assert source_report["source_fingerprint_match"] is True
            expected_timing_keys = {
                "source_fingerprint_and_load",
                "preflight",
                "normalize_and_bake",
                "merge",
                "write_sparse",
                "roundtrip_validate",
                "copy_and_hash_images",
                "write_audit_artifacts",
                "total",
            }
            assert set(report["stage_timings_seconds"]) == expected_timing_keys
            assert all(value >= 0.0 for value in report["stage_timings_seconds"].values())
            assert set(report["output_file_sha256"]) >= {
                "images/a.jpg",
                "images/b.jpg",
                f"sparse/0/{expected_names[3]}",
            }
            restored = load_sparse_model(candidate_root, input_format=output_format.value)
            assert restored.points.count == 1
            expected_positions = (
                source_model.points.positions
                if restore_import_axes
                else np.asarray(((0.0, 1.0, 1.0),), dtype=np.float64)
            )
            assert np.allclose(
                restored.points.positions,
                expected_positions,
                rtol=0.0,
                atol=1.0e-12,
            )
            if restore_import_axes:
                assert np.allclose(
                    restored.images.image_records[1].cam_from_world,
                    source_model.images.image_records[1].cam_from_world,
                    rtol=0.0,
                    atol=1.0e-12,
                )
            with np.load(candidate_root / "merge_id_map.npz", allow_pickle=False) as arrays:
                assert arrays.files


def test_worker_preflight_only_never_creates_candidate() -> None:
    """preflight-only worker가 source를 다시 읽되 output candidate를 만들지 않는지 확인합니다."""

    from dataclasses import replace

    from tools.align.colmap_io import discover_sparse_model, source_fingerprint
    from tools.align.colmap_io.binary_writer import write_binary_model
    from tools.align.merge_export.types import MergeExportSnapshot, MergePhase
    from tools.align.merge_export.worker import run_worker_job

    with TemporaryDirectory(prefix="colmap_align_merge_preflight_worker_") as directory:
        root = Path(directory)
        source_root = root / "source"
        source_model = _modern_model(source_root / "sparse" / "0", model_id="anchor")
        write_binary_model(source_model, source_root / "sparse" / "0")
        expected_source_fingerprint = source_fingerprint(
            discover_sparse_model(source_root, input_format="BINARY")
        )
        source = replace(
            _source_snapshot(
                source_model,
                is_anchor=True,
                load_order=0,
                expected_source_fingerprint=expected_source_fingerprint,
            ),
            source_path=source_root,
        )
        candidate_root = root / ".colmap_align_export_preflight-only"
        snapshot = MergeExportSnapshot(
            job_id="preflight-only",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            candidate_root=candidate_root,
            anchor_model_id="anchor",
            source_models=(source,),
            runtime_fingerprint="runtime-fingerprint",
        )
        job_directory = root / "job"
        _write_worker_manifest(job_directory, snapshot, preflight_only=True)
        result = run_worker_job(job_directory)
        assert result.phase is MergePhase.PREFLIGHT_READY
        assert (job_directory / "preflight.json").is_file()
        assert not candidate_root.exists()


def test_worker_rejects_source_changed_after_import() -> None:
    """현재 source SHA-256이 Import 당시 값과 다르면 candidate 생성 전에 실패하는지 확인합니다."""

    from dataclasses import replace

    from tools.align.colmap_io.binary_writer import write_binary_model
    from tools.align.merge_export.types import MergeExportSnapshot, MergePhase
    from tools.align.merge_export.worker import run_worker_job

    with TemporaryDirectory(prefix="colmap_align_merge_fingerprint_") as directory:
        root = Path(directory)
        source_root = root / "source"
        source_model = _modern_model(source_root / "sparse" / "0", model_id="anchor")
        write_binary_model(source_model, source_root / "sparse" / "0")
        source = replace(
            _source_snapshot(source_model, is_anchor=True, load_order=0),
            source_path=source_root,
        )
        candidate_root = root / ".colmap_align_export-fingerprint-mismatch"
        snapshot = MergeExportSnapshot(
            job_id="fingerprint-mismatch",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            candidate_root=candidate_root,
            anchor_model_id="anchor",
            source_models=(source,),
            runtime_fingerprint="runtime-fingerprint",
        )
        job_directory = root / "job"
        _write_worker_manifest(job_directory, snapshot, preflight_only=False)
        result = run_worker_job(job_directory)
        assert result.phase is MergePhase.FAILED
        assert result.error is not None
        assert "fingerprint" in result.error.cause
        assert not candidate_root.exists()


def test_worker_cancellation_never_creates_candidate() -> None:
    """취소 sentinel이 source 읽기 전에 감지되면 candidate 없이 종료하는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportSnapshot, MergePhase
    from tools.align.merge_export.worker import run_worker_job

    with TemporaryDirectory(prefix="colmap_align_merge_cancel_worker_") as directory:
        root = Path(directory)
        model = _modern_model(root / "source", model_id="anchor")
        candidate_root = root / ".colmap_align_export_cancelled"
        snapshot = MergeExportSnapshot(
            job_id="cancelled",
            protocol_version="merge-test-v1",
            algorithm_version="merge-test-v1",
            output_root=root / "output",
            candidate_root=candidate_root,
            anchor_model_id="anchor",
            source_models=(_source_snapshot(model, is_anchor=True, load_order=0),),
            runtime_fingerprint="runtime-fingerprint",
        )
        job_directory = root / "job"
        _write_worker_manifest(job_directory, snapshot, preflight_only=False)
        (job_directory / "cancel.request").write_text("cancel", encoding="utf-8")
        result = run_worker_job(job_directory)
        assert result.phase is MergePhase.CANCELLED
        assert (job_directory / "error.json").is_file()
        assert not candidate_root.exists()
        assert not (root / "output").exists()


def run_all() -> None:
    """Blender background 한 프로세스에서 merge engine·worker 합성 테스트를 실행합니다."""

    _add_source_path()
    test_engine_keeps_observation_tracks_and_writes_deterministic_id_maps()
    test_engine_skips_reprojection_when_basenames_do_not_overlap()
    test_engine_appends_different_point2d_counts_with_source_offsets()
    test_preflight_warns_for_reencoded_duplicate_and_keeps_anchor_representative()
    test_preflight_warns_and_uses_anchor_for_independent_camera_and_pose()
    test_preflight_still_rejects_duplicate_with_different_resolution()
    test_engine_offsets_shared_image_features_without_merging_point3d()
    test_engine_applies_cumulative_offsets_for_three_sources()
    test_preflight_explains_inconsistent_modern_rig_frame_pose()
    test_worker_writes_candidate_and_waits_for_main_atomic_finalize()
    test_worker_preflight_only_never_creates_candidate()
    test_worker_rejects_source_changed_after_import()
    test_worker_cancellation_never_creates_candidate()
    print("test_merge_export_engine: 통과")


if __name__ == "__main__":
    run_all()
