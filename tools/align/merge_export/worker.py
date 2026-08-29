"""숨김 Blender worker에서 구조 통합 COLMAP sparse model을 기록합니다."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from time import monotonic, sleep
from typing import Any, Callable, Mapping, Sequence

import numpy as np


if __package__ in {None, ""}:
    # Blender --python 실행에서도 설치된 애드온의 src 루트를 먼저 찾습니다.
    _SOURCE_ROOT = Path(__file__).resolve().parents[2]
    if str(_SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(_SOURCE_ROOT))
    from align.alignment.types import Sim3Transform
    from align.colmap_io import (
        ColmapModelData,
        ImageTable,
        PointTable,
        create_incremental_sparse_model_reader,
        discover_sparse_model,
        load_sparse_model,
        validate_model,
    )
    from align.colmap_io.binary_writer import write_binary_model
    from align.colmap_io.text_writer import write_text_model
    from align.colmap_io.fingerprint import SourceFingerprintJob
    from align.colmap_io.validation import calculate_bounds
    from align.merge_export.merge_engine import MergeEngineResult, merge_baked_models
    from align.merge_export.preflight import (
        LoadedMergeSource,
        normalized_relative_image_name,
        run_preflight,
        source_image_path,
    )
    from align.merge_export.transforms import (
        compose_anchor_colmap_output_transform,
        compose_cam_from_world,
        compose_project_world_transform,
        normalize_legacy_rigs_and_frames,
        quaternion_to_rotation_matrix,
        transform_frame_records,
        transform_image_record,
        transform_point_positions,
        transform_rig_records,
    )
    from align.merge_export.types import (
        MergeCancelledError,
        MergeExportProgress,
        MergeExportResult,
        MergeExportSnapshot,
        MergeIssue,
        MergeOutputFormat,
        MergePhase,
        MergeProtocolError,
        MergeSourceModelSnapshot,
        MergeStage,
    )
else:
    from ..alignment.types import Sim3Transform
    from ..colmap_io import (
        ColmapModelData,
        ImageTable,
        PointTable,
        create_incremental_sparse_model_reader,
        discover_sparse_model,
        load_sparse_model,
        validate_model,
    )
    from ..colmap_io.binary_writer import write_binary_model
    from ..colmap_io.text_writer import write_text_model
    from ..colmap_io.fingerprint import SourceFingerprintJob
    from ..colmap_io.validation import calculate_bounds
    from .merge_engine import MergeEngineResult, merge_baked_models
    from .preflight import (
        LoadedMergeSource,
        normalized_relative_image_name,
        run_preflight,
        source_image_path,
    )
    from .transforms import (
        compose_anchor_colmap_output_transform,
        compose_cam_from_world,
        compose_project_world_transform,
        normalize_legacy_rigs_and_frames,
        quaternion_to_rotation_matrix,
        transform_frame_records,
        transform_image_record,
        transform_point_positions,
        transform_rig_records,
    )
    from .types import (
        MergeCancelledError,
        MergeExportProgress,
        MergeExportResult,
        MergeExportSnapshot,
        MergeIssue,
        MergeOutputFormat,
        MergePhase,
        MergeProtocolError,
        MergeSourceModelSnapshot,
        MergeStage,
    )


_MANIFEST_NAME = "manifest.json"
_PROGRESS_NAME = "progress.json"
_PREFLIGHT_NAME = "preflight.json"
_RESULT_NAME = "result.json"
_ERROR_NAME = "error.json"
_CANCEL_NAME = "cancel.request"
_REPORT_NAME = "merge_report.json"
_ID_MAP_NAME = "merge_id_map.npz"
_SOURCE_READ_STEP_RECORDS = 50_000
_IMAGE_COPY_CHUNK_BYTES = 4 * 1024 * 1024
_IMAGE_COPY_PROGRESS_INTERVAL_SECONDS = 0.25
_PROGRESS_WRITE_INTERVAL_SECONDS = 0.25
_POSE_ATOL = 1.0e-8
_PROGRESS_WRITE_ATTEMPTS = 3
_PROGRESS_WRITE_RETRY_DELAY_SECONDS = 0.03
_PROGRESS_FILE_RACE_WINERRORS = frozenset({5, 32})


@dataclass(slots=True)
class WorkerJobFiles:
    """한 전용 worker job 디렉터리에서 허용된 고정 파일 경로를 보관합니다."""

    job_directory: Path
    last_progress_write_at: float = field(default=-1.0, init=False)
    last_progress_stage: str = field(default="", init=False)
    pending_progress: dict[str, Any] | None = field(default=None, init=False)
    progress_write_count: int = field(default=0, init=False)

    @property
    def manifest_path(self) -> Path:
        """backend가 기록한 immutable manifest 경로를 반환합니다."""

        return self.job_directory / _MANIFEST_NAME

    @property
    def progress_path(self) -> Path:
        """worker가 원자적으로 갱신하는 진행률 경로를 반환합니다."""

        return self.job_directory / _PROGRESS_NAME

    @property
    def preflight_path(self) -> Path:
        """worker가 기록하는 source 재검사 결과 경로를 반환합니다."""

        return self.job_directory / _PREFLIGHT_NAME

    @property
    def result_path(self) -> Path:
        """성공 또는 preflight-only 완료 결과 경로를 반환합니다."""

        return self.job_directory / _RESULT_NAME

    @property
    def error_path(self) -> Path:
        """실패·취소의 구조화된 결과 경로를 반환합니다."""

        return self.job_directory / _ERROR_NAME

    @property
    def cancel_path(self) -> Path:
        """메인 Blender가 만드는 취소 sentinel 경로를 반환합니다."""

        return self.job_directory / _CANCEL_NAME


@dataclass(frozen=True, slots=True)
class SourceAudit:
    """worker가 source 파일 전후 stat·SHA-256을 확인한 감사 결과입니다."""

    model_id: str
    source_path: Path
    source_format: str
    expected_source_fingerprint: str
    source_fingerprint: str
    file_stats: tuple[tuple[str, int, int], ...]

    def to_mapping(self) -> dict[str, Any]:
        """merge report의 source 감사 항목으로 변환합니다."""

        return {
            "model_id": self.model_id,
            "source_path": str(self.source_path),
            "source_format": self.source_format,
            "expected_source_fingerprint": self.expected_source_fingerprint,
            "actual_source_fingerprint": self.source_fingerprint,
            "source_fingerprint_match": (
                self.source_fingerprint == self.expected_source_fingerprint
            ),
            "file_stats": [
                {"name": name, "size": size, "mtime_ns": mtime_ns}
                for name, size, mtime_ns in self.file_stats
            ],
        }


@dataclass(frozen=True, slots=True)
class _ImageCopyEntry:
    """한 output Image.name과 대표 원본 파일의 검증된 복사 계획입니다."""

    output_image_id: int
    relative_name: str
    source_path: Path
    target_path: Path
    source_size: int
    source_mtime_ns: int


def _strict_job_directory(value: str | Path) -> Path:
    """존재하는 worker job 폴더만 절대 경로로 고정합니다."""

    directory = Path(value).resolve(strict=True)
    if not directory.is_dir():
        raise MergeProtocolError("구조 통합 worker job 경로가 폴더가 아닙니다.")
    return directory


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """같은 폴더 임시 파일을 fsync한 뒤 os.replace로 원자 교체합니다."""

    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """NaN 없이 정렬된 UTF-8 JSON 객체를 원자적으로 기록합니다."""

    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MergeProtocolError("worker JSON에 기록할 수 없는 값이 있습니다.") from error
    _atomic_write_bytes(path, encoded)


def _is_transient_progress_file_race(error: OSError) -> bool:
    """Windows의 progress.json 접근 거부·공유 위반만 일시 경합으로 판별합니다."""

    return getattr(error, "winerror", None) in _PROGRESS_FILE_RACE_WINERRORS


def _commit_progress_json(files: WorkerJobFiles, payload: Mapping[str, Any]) -> bool:
    """진행률 기록의 모든 파일 오류를 보조 채널 실패로 제한하고 성공 여부를 반환합니다."""

    for attempt in range(_PROGRESS_WRITE_ATTEMPTS):
        try:
            _atomic_write_json(files.progress_path, payload)
            return True
        except OSError as error:
            if _is_transient_progress_file_race(error) and attempt + 1 < _PROGRESS_WRITE_ATTEMPTS:
                sleep(_PROGRESS_WRITE_RETRY_DELAY_SECONDS)
                continue
            return False
    return False


def _write_progress_json(
    files: WorkerJobFiles,
    payload: Mapping[str, Any],
    *,
    force: bool = False,
) -> None:
    """단계 변경·시간 간격·강제 완료 시점에만 최신 progress를 best-effort로 기록합니다."""

    mapping = dict(payload)
    stage = str(mapping.get("stage", ""))
    now = monotonic()
    if files.pending_progress is not None and stage != files.last_progress_stage:
        if _commit_progress_json(files, files.pending_progress):
            files.last_progress_write_at = now
            files.progress_write_count += 1
        files.pending_progress = None
    same_stage = stage == files.last_progress_stage
    if (
        not force
        and same_stage
        and files.last_progress_write_at >= 0.0
        and now - files.last_progress_write_at < _PROGRESS_WRITE_INTERVAL_SECONDS
    ):
        files.pending_progress = mapping
        return
    if _commit_progress_json(files, mapping):
        files.last_progress_write_at = now
        files.last_progress_stage = stage
        files.pending_progress = None
        files.progress_write_count += 1
    else:
        files.pending_progress = mapping


def _read_json_object(path: Path) -> dict[str, Any]:
    """완성된 JSON 객체만 읽고 숫자·최상위 형식을 엄격히 확인합니다."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MergeProtocolError(f"worker JSON을 읽지 못했습니다: {path.name}") from error
    if not isinstance(value, dict):
        raise MergeProtocolError(f"worker JSON 최상위 값은 객체여야 합니다: {path.name}")
    _validate_json_values(value)
    return value


def _validate_json_values(value: Any) -> None:
    """외부 manifest JSON의 비유한 수치와 지원하지 않는 값을 거부합니다."""

    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not np.isfinite(value):
            raise MergeProtocolError("worker JSON에 NaN 또는 무한대가 있습니다.")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_values(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _validate_json_values(item)
        return
    raise MergeProtocolError("worker JSON에 지원하지 않는 값이 있습니다.")


def _load_manifest(files: WorkerJobFiles) -> tuple[MergeExportSnapshot, bool]:
    """backend wrapper manifest에서 immutable snapshot과 preflight-only flag를 복원합니다."""

    manifest = _read_json_object(files.manifest_path)
    if set(manifest).difference({"snapshot", "preflight_only"}):
        raise MergeProtocolError("worker manifest에 허용하지 않는 최상위 항목이 있습니다.")
    if "snapshot" not in manifest or "preflight_only" not in manifest:
        raise MergeProtocolError("worker manifest에 snapshot 또는 preflight_only가 없습니다.")
    if not isinstance(manifest["preflight_only"], bool):
        raise MergeProtocolError("worker manifest preflight_only는 bool이어야 합니다.")
    snapshot = MergeExportSnapshot.from_mapping(manifest["snapshot"])
    return snapshot, manifest["preflight_only"]


def _raise_if_cancelled(files: WorkerJobFiles) -> None:
    """long-running source·track·I/O 단계에서 취소 sentinel을 즉시 확인합니다."""

    if files.cancel_path.exists():
        raise MergeCancelledError("사용자가 구조 통합 내보내기 작업을 취소했습니다.")


def _emit_progress(
    files: WorkerJobFiles,
    snapshot: MergeExportSnapshot,
    stage: MergeStage,
    fraction: float,
    message: str,
    *,
    counts: Mapping[str, int] | None = None,
    force: bool = False,
) -> None:
    """main Blender poll용 진행률을 기록하되 일시 경합은 작업 실패로 전파하지 않습니다."""

    progress = MergeExportProgress(
        job_id=snapshot.job_id,
        stage=stage,
        fraction=fraction,
        message=message,
        counts=counts or {},
    )
    _write_progress_json(files, progress.to_mapping(), force=force)


def _write_preflight_progress(
    files: WorkerJobFiles,
    progress: MergeExportProgress,
    *,
    preflight_only: bool,
) -> None:
    """preflight 진행률도 일시 경합을 건너뛰며 전체 worker 구간에 맞춰 기록합니다."""

    start_fraction = 0.47
    end_fraction = 0.99 if preflight_only else 0.52
    mapping = progress.to_mapping()
    mapping["fraction"] = start_fraction + (
        end_fraction - start_fraction
    ) * progress.fraction
    _write_progress_json(files, mapping)


def _stat_selected_files(layout: Any) -> tuple[tuple[str, int, int], ...]:
    """선택된 3·5개 source 파일의 size·mtime_ns snapshot을 정렬해 반환합니다."""

    stats: list[tuple[str, int, int]] = []
    for path in layout.selected_files:
        stat = path.stat()
        stats.append((path.name, int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(stats)


def _read_source_model(
    layout: Any,
    source: Any,
    files: WorkerJobFiles,
) -> ColmapModelData:
    """취소 확인 가능한 incremental reader로 source 모델을 완전히 다시 적재합니다."""

    reader = create_incremental_sparse_model_reader(
        layout,
        model_id=source.model_id,
        display_name=source.display_name,
        geometry_revision=source.geometry_revision,
    )
    try:
        while not reader.is_completed:
            _raise_if_cancelled(files)
            reader.step(max_records=_SOURCE_READ_STEP_RECORDS)
        model = reader.model
    finally:
        reader.close()
    return model


def _fingerprint_and_load_sources(
    files: WorkerJobFiles,
    snapshot: MergeExportSnapshot,
) -> tuple[tuple[LoadedMergeSource, ...], tuple[SourceAudit, ...]]:
    """worker에서 source SHA-256·전후 stat을 확인하고 immutable snapshot 모델을 다시 적재합니다."""

    loaded: list[LoadedMergeSource] = []
    audits: list[SourceAudit] = []
    total = len(snapshot.source_models)
    for ordinal, source in enumerate(snapshot.source_models):
        _raise_if_cancelled(files)
        _emit_progress(
            files,
            snapshot,
            MergeStage.FINGERPRINT_SOURCES,
            0.03 + 0.22 * (ordinal / total),
            "COLMAP source 파일의 변경 여부와 SHA-256을 검사하고 있습니다.",
            counts={"source_models": ordinal, "total_source_models": total},
        )
        layout = discover_sparse_model(source.source_path, input_format=source.source_format.upper())
        before_stats = _stat_selected_files(layout)
        fingerprint_job = SourceFingerprintJob(layout)
        try:
            while not fingerprint_job.done:
                _raise_if_cancelled(files)
                fingerprint_job.step(time_budget_seconds=0.20)
            fingerprint = fingerprint_job.result
        finally:
            fingerprint_job.close()
        if _stat_selected_files(layout) != before_stats:
            raise MergeProtocolError("COLMAP source 파일이 SHA-256 계산 중 변경되었습니다.")
        if fingerprint != source.expected_source_fingerprint:
            raise MergeProtocolError(
                "COLMAP source fingerprint가 Import 당시 값과 다릅니다: "
                f"{source.display_name}"
            )
        _emit_progress(
            files,
            snapshot,
            MergeStage.READ_AND_VALIDATE_SOURCES,
            0.25 + 0.25 * (ordinal / total),
            "COLMAP source의 camera·image·track 구조를 다시 읽고 있습니다.",
            counts={"source_models": ordinal, "total_source_models": total},
        )
        model = _read_source_model(layout, source, files)
        if _stat_selected_files(layout) != before_stats:
            raise MergeProtocolError("COLMAP source 파일이 sparse model 파싱 중 변경되었습니다.")
        loaded.append(LoadedMergeSource(ordinal=ordinal, snapshot=source, model=model))
        audits.append(
            SourceAudit(
                model_id=source.model_id,
                source_path=layout.source_path,
                source_format=layout.selected_format,
                expected_source_fingerprint=source.expected_source_fingerprint,
                source_fingerprint=fingerprint,
                file_stats=before_stats,
            )
        )
    return tuple(loaded), tuple(audits)


def _baked_image_table(model: ColmapModelData, transform: Sim3Transform) -> ImageTable:
    """원본 points2D 배열은 보존하고 image pose만 최종 출력 좌표로 변환합니다."""

    records = {
        image_id: transform_image_record(model.images.image_records[image_id], transform)
        for image_id in sorted(model.images.image_records)
    }
    return ImageTable(
        image_records=records,
        point2d_offsets=model.images.point2d_offsets,
        point2d_xy=model.images.point2d_xy,
        point2d_point3d_ids=model.images.point2d_point3d_ids,
    )


def _baked_point_table(model: ColmapModelData, transform: Sim3Transform) -> PointTable:
    """원본 Point3D ID·track은 보존하고 XYZ만 최종 출력 좌표로 굽습니다."""

    return PointTable(
        point_ids=model.points.point_ids,
        positions=transform_point_positions(model.points.positions, transform),
        colors=model.points.colors,
        errors=model.points.errors,
        track_offsets=model.points.track_offsets,
        track_image_ids=model.points.track_image_ids,
        track_point2d_indices=model.points.track_point2d_indices,
        display_indices=model.points.display_indices,
    )


def _assert_baked_modern_pose_consistency(
    model: ColmapModelData,
) -> None:
    """modern rig/frame 합성 pose와 직접 변환 image pose가 같은지 명시적으로 확인합니다."""

    for frame in model.frames.values():
        rig = model.rigs[frame.rig_id]
        sensors = {(sensor.sensor_type, sensor.sensor_id): sensor for sensor in rig.sensors}
        for data in frame.data_ids:
            if data.sensor_type != "CAMERA":
                raise MergeProtocolError("IMU frame data는 첫 구조 통합에서 지원하지 않습니다.")
            sensor = sensors.get((data.sensor_type, data.sensor_id))
            image = model.images.image_records.get(data.data_id)
            if sensor is None or image is None:
                raise MergeProtocolError("modern rig/frame이 존재하지 않는 sensor 또는 image를 참조합니다.")
            composed = compose_cam_from_world(sensor.sensor_from_rig, frame.rig_from_world)
            composed_rotation = quaternion_to_rotation_matrix(composed[:4])
            image_rotation = quaternion_to_rotation_matrix(image.cam_from_world[:4])
            if not (
                np.allclose(composed_rotation, image_rotation, rtol=0.0, atol=_POSE_ATOL)
                and np.allclose(composed[4:], image.cam_from_world[4:], rtol=0.0, atol=_POSE_ATOL)
            ):
                raise MergeProtocolError(
                    "최종 좌표 bake 뒤 modern rig/frame pose와 image pose가 일치하지 않습니다."
                )


def _anchor_source_snapshot(snapshot: MergeExportSnapshot) -> MergeSourceModelSnapshot:
    """검증된 내보내기 snapshot에서 유일한 Anchor source를 반환합니다."""

    return next(source for source in snapshot.source_models if source.model_id == snapshot.anchor_model_id)


def _bake_source(
    source: LoadedMergeSource,
    anchor_display_matrix_float64: object,
    *,
    restore_import_axes: bool,
) -> LoadedMergeSource:
    """선택한 출력 축 기준으로 legacy 정규화와 확정 변환을 정확히 한 번 수행합니다."""

    snapshot = source.snapshot
    alignment = Sim3Transform(matrix_float64=snapshot.alignment_matrix_float64)
    transform = (
        compose_anchor_colmap_output_transform(
            alignment,
            snapshot.display_matrix_float64,
            anchor_display_matrix_float64,
        )
        if restore_import_axes
        else compose_project_world_transform(
            alignment,
            snapshot.display_matrix_float64,
        )
    )
    original = source.model
    if original.rigs:
        baked_rigs = transform_rig_records(original.rigs, transform)
        baked_frames = transform_frame_records(original.frames, transform)
    else:
        # legacy frame은 여기서 transform을 적용해 만든 뒤 다시 변환하지 않습니다.
        legacy_rigs, baked_frames = normalize_legacy_rigs_and_frames(original, transform=transform)
        baked_rigs = transform_rig_records(legacy_rigs, transform)
    baked_images = _baked_image_table(original, transform)
    baked_points = _baked_point_table(original, transform)
    bounds_min, bounds_max = calculate_bounds(baked_points.positions)
    baked = ColmapModelData(
        model_id=original.model_id,
        display_name=original.display_name,
        source_path=original.source_path,
        source_format=original.source_format,
        cameras=original.cameras,
        images=baked_images,
        rigs=baked_rigs,
        frames=baked_frames,
        points=baked_points,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        geometry_revision=original.geometry_revision,
    )
    validate_model(baked)
    _assert_baked_modern_pose_consistency(baked)
    return LoadedMergeSource(ordinal=source.ordinal, snapshot=snapshot, model=baked)


def _safe_candidate_root(snapshot: MergeExportSnapshot) -> Path:
    """backend가 제공한 output 부모의 전용 candidate 경로만 worker 기록 대상으로 허용합니다."""

    if snapshot.candidate_root is None:
        raise MergeProtocolError("export worker manifest에 candidate_root가 없습니다.")
    output_root = snapshot.output_root.resolve(strict=False)
    candidate = snapshot.candidate_root.resolve(strict=False)
    if candidate.parent != output_root.parent:
        raise MergeProtocolError("candidate_root는 output_root와 같은 부모의 형제 폴더여야 합니다.")
    prefix = f".colmap_align_export_{snapshot.job_id}"
    if candidate.name != prefix:
        raise MergeProtocolError("candidate_root 이름이 허용한 export job prefix와 다릅니다.")
    if output_root.exists():
        if not snapshot.overwrite_existing_output:
            raise MergeProtocolError("사용자 덮어쓰기 승인 없이 출력 폴더가 이미 존재합니다.")
        if output_root.is_symlink() or not output_root.is_dir():
            raise MergeProtocolError("덮어쓸 출력 경로는 심볼릭 링크가 아닌 폴더여야 합니다.")
    if candidate.exists():
        raise MergeProtocolError("candidate 폴더가 이미 존재해 안전하게 내보낼 수 없습니다.")
    if not output_root.parent.is_dir():
        raise MergeProtocolError("출력 parent 폴더가 없어 candidate를 만들 수 없습니다.")
    return candidate


def _create_candidate(snapshot: MergeExportSnapshot) -> Path:
    """preflight 통과 뒤에만 전용 후보 폴더를 새로 만들고 원본을 건드리지 않습니다."""

    candidate = _safe_candidate_root(snapshot)
    candidate.mkdir(mode=0o700)
    return candidate


def _cleanup_candidate(snapshot: MergeExportSnapshot) -> None:
    """실패·취소 시 검증된 전용 candidate만 제거하고 output/source는 건드리지 않습니다."""

    if snapshot.candidate_root is None:
        return
    output_root = snapshot.output_root.resolve(strict=False)
    candidate = snapshot.candidate_root.resolve(strict=False)
    prefix = f".colmap_align_export_{snapshot.job_id}"
    if candidate.parent != output_root.parent or candidate.name != prefix:
        return
    if candidate.exists() and candidate.is_dir():
        shutil.rmtree(candidate)


def _sha256_file(
    path: Path,
    *,
    cancel_requested: Callable[[], None] | None = None,
) -> str:
    """완성된 audit 파일의 SHA-256을 계산하며 긴 읽기 중 취소를 확인합니다."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancel_requested is not None:
                cancel_requested()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_image_copy_plan(
    candidate: Path,
    raw_sources: Sequence[LoadedMergeSource],
    engine_result: MergeEngineResult,
) -> tuple[_ImageCopyEntry, ...]:
    """output IMAGE_ID별 대표 원본을 안전한 candidate/images 대상으로 연결합니다."""

    source_by_ordinal = {source.ordinal: source for source in raw_sources}
    representatives: dict[int, tuple[LoadedMergeSource, int]] = {}
    for (source_ordinal, old_image_id), output_image_id in sorted(
        engine_result.id_maps.image_ids.items()
    ):
        try:
            source = source_by_ordinal[source_ordinal]
        except KeyError as error:
            raise MergeProtocolError(
                "이미지 ID map이 존재하지 않는 source ordinal을 참조합니다."
            ) from error
        representatives.setdefault(output_image_id, (source, old_image_id))

    output_images = engine_result.model.images.image_records
    if set(representatives) != set(output_images):
        raise MergeProtocolError("output 이미지와 대표 원본 이미지 ID 집합이 다릅니다.")

    image_root = (candidate / "images").resolve(strict=False)
    targets: set[str] = set()
    entries: list[_ImageCopyEntry] = []
    for output_image_id in sorted(output_images):
        source, old_image_id = representatives[output_image_id]
        try:
            original = source.model.images.image_records[old_image_id]
        except KeyError as error:
            raise MergeProtocolError(
                "이미지 ID map이 존재하지 않는 원본 IMAGE_ID를 참조합니다."
            ) from error
        relative_name = normalized_relative_image_name(output_images[output_image_id].name)
        relative_path = Path(*PurePosixPath(relative_name).parts)
        target_path = (image_root / relative_path).resolve(strict=False)
        try:
            target_path.relative_to(image_root)
        except ValueError as error:
            raise MergeProtocolError(
                "output Image.name이 candidate images 폴더 밖을 가리킵니다."
            ) from error
        target_key = relative_name.casefold()
        if target_key in targets:
            raise MergeProtocolError(
                f"대소문자 구분 없이 중복되는 output 이미지 경로가 있습니다: {relative_name}"
            )
        targets.add(target_key)
        source_path = source_image_path(source.snapshot, original.name)
        source_stat = source_path.stat()
        entries.append(
            _ImageCopyEntry(
                output_image_id=output_image_id,
                relative_name=relative_name,
                source_path=source_path,
                target_path=target_path,
                source_size=int(source_stat.st_size),
                source_mtime_ns=int(source_stat.st_mtime_ns),
            )
        )
    return tuple(entries)


def _copy_dataset_images(
    files: WorkerJobFiles,
    snapshot: MergeExportSnapshot,
    candidate: Path,
    entries: Sequence[_ImageCopyEntry],
) -> tuple[dict[str, str], int]:
    """대표 이미지를 스트리밍 복사하며 해시·원본 변경·취소·진행률을 확인합니다."""

    image_root = candidate / "images"
    image_root.mkdir()
    total_bytes = sum(entry.source_size for entry in entries)
    copied_bytes = 0
    hashes: dict[str, str] = {}
    _emit_progress(
        files,
        snapshot,
        MergeStage.COPY_IMAGES,
        0.87,
        "통합 COLMAP 데이터셋의 images 파일을 복사하고 있습니다.",
        counts={
            "image_files": 0,
            "total_image_files": len(entries),
            "image_bytes": 0,
            "total_image_bytes": total_bytes,
        },
    )
    last_progress_at = monotonic()
    for image_index, entry in enumerate(entries, start=1):
        _raise_if_cancelled(files)
        entry.target_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with entry.source_path.open("rb") as source_stream, entry.target_path.open("xb") as target_stream:
            while True:
                _raise_if_cancelled(files)
                chunk = source_stream.read(_IMAGE_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                target_stream.write(chunk)
                digest.update(chunk)
                copied_bytes += len(chunk)
                current = monotonic()
                if current - last_progress_at >= _IMAGE_COPY_PROGRESS_INTERVAL_SECONDS:
                    fraction = 1.0 if total_bytes == 0 else copied_bytes / total_bytes
                    _emit_progress(
                        files,
                        snapshot,
                        MergeStage.COPY_IMAGES,
                        0.87 + 0.10 * fraction,
                        "통합 COLMAP 데이터셋의 images 파일을 복사하고 있습니다.",
                        counts={
                            "image_files": image_index - 1,
                            "total_image_files": len(entries),
                            "image_bytes": copied_bytes,
                            "total_image_bytes": total_bytes,
                        },
                    )
                    last_progress_at = current
            target_stream.flush()
            os.fsync(target_stream.fileno())
        source_stat = entry.source_path.stat()
        if (
            int(source_stat.st_size) != entry.source_size
            or int(source_stat.st_mtime_ns) != entry.source_mtime_ns
            or entry.target_path.stat().st_size != entry.source_size
        ):
            raise MergeProtocolError(
                f"이미지 복사 중 원본 또는 출력 크기가 변경되었습니다: {entry.relative_name}"
            )
        hashes[f"images/{entry.relative_name}"] = digest.hexdigest()
    _emit_progress(
        files,
        snapshot,
        MergeStage.COPY_IMAGES,
        0.97,
        "통합 COLMAP 데이터셋의 images 파일 복사를 완료했습니다.",
        counts={
            "image_files": len(entries),
            "total_image_files": len(entries),
            "image_bytes": copied_bytes,
            "total_image_bytes": total_bytes,
        },
        force=True,
    )
    return hashes, copied_bytes


def _atomic_write_npz(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    """object 배열 없이 ID audit NPZ를 기록하고 allow_pickle=False 재읽기를 강제합니다."""

    for key, array in arrays.items():
        if np.asarray(array).dtype == np.dtype(object):
            raise MergeProtocolError(f"ID audit NPZ에 object 배열을 기록할 수 없습니다: {key}")
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.savez(stream, **dict(arrays))
            stream.flush()
            os.fsync(stream.fileno())
        with np.load(temporary, allow_pickle=False) as loaded:
            if set(loaded.files) != set(arrays):
                raise MergeProtocolError("ID audit NPZ 재읽기 항목이 기록 요청과 다릅니다.")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _roundtrip_validate(
    candidate: Path,
    expected: ColmapModelData,
    output_format: MergeOutputFormat,
) -> None:
    """writer 결과를 현재 reader로 다시 적재해 구조·수치 핵심 항목을 비교합니다."""

    restored = load_sparse_model(
        candidate,
        input_format=output_format.value,
        model_id=expected.model_id,
        display_name=expected.display_name,
        geometry_revision=expected.geometry_revision,
    )
    if (
        len(restored.rigs) != len(expected.rigs)
        or len(restored.cameras) != len(expected.cameras)
        or len(restored.frames) != len(expected.frames)
        or restored.images.count != expected.images.count
        or restored.points.count != expected.points.count
    ):
        raise MergeProtocolError("기록한 sparse model의 reader 왕복 count가 다릅니다.")
    if not (
        np.array_equal(restored.points.point_ids, expected.points.point_ids)
        and np.array_equal(restored.points.track_image_ids, expected.points.track_image_ids)
        and np.array_equal(restored.points.track_point2d_indices, expected.points.track_point2d_indices)
        and np.allclose(restored.points.positions, expected.points.positions, rtol=0.0, atol=0.0)
        and np.allclose(restored.points.errors, expected.points.errors, rtol=0.0, atol=0.0)
        and np.array_equal(restored.images.point2d_point3d_ids, expected.images.point2d_point3d_ids)
        and np.allclose(restored.images.point2d_xy, expected.images.point2d_xy, rtol=0.0, atol=0.0)
    ):
        raise MergeProtocolError("기록한 sparse model의 reader 왕복 ID·좌표·track이 다릅니다.")


def _counts(model: ColmapModelData) -> dict[str, int]:
    """UI와 report가 공통으로 쓰는 작은 output count mapping을 반환합니다."""

    return {
        "rigs": len(model.rigs),
        "cameras": len(model.cameras),
        "frames": len(model.frames),
        "images": model.images.count,
        "points3d": model.points.count,
        "observations": int(model.points.track_image_ids.shape[0]),
    }


def _input_counts(sources: Sequence[LoadedMergeSource]) -> dict[str, int]:
    """source 재적재 직후의 실제 구조·관측 개수를 report용으로 합산합니다."""

    return {
        "source_models": len(sources),
        "rigs": sum(len(source.model.rigs) for source in sources),
        "cameras": sum(len(source.model.cameras) for source in sources),
        "frames": sum(len(source.model.frames) for source in sources),
        "images": sum(source.model.images.count for source in sources),
        "points3d": sum(source.model.points.count for source in sources),
        "observations": sum(int(source.model.points.track_image_ids.shape[0]) for source in sources),
    }


def _connected_component_summary(model: ColmapModelData) -> dict[str, int]:
    """출력 image 관측 그래프의 연결 성분 수와 최대 성분 크기를 실제로 계산합니다."""

    image_ids = tuple(sorted(model.images.image_records))
    index_by_image = {image_id: index for index, image_id in enumerate(image_ids)}
    parent = list(range(len(image_ids)))

    def find(index: int) -> int:
        """작은 image graph용 path compression root를 반환합니다."""

        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        """한 Point3D track으로 연결된 두 이미지를 같은 graph 성분으로 합칩니다."""

        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    images_with_tracks: set[int] = set()
    for point_index in range(model.points.count):
        track = model.points.track_range(point_index)
        if track.stop - track.start < 2:
            continue
        first_image = int(model.points.track_image_ids[track.start])
        images_with_tracks.add(first_image)
        for track_index in range(track.start + 1, track.stop):
            next_image = int(model.points.track_image_ids[track_index])
            images_with_tracks.add(next_image)
            union(index_by_image[first_image], index_by_image[next_image])
    sizes: dict[int, int] = {}
    for image_id in image_ids:
        root = find(index_by_image[image_id])
        sizes[root] = sizes.get(root, 0) + 1
    return {
        "image_graph_components": len(sizes),
        "largest_image_component_size": max(sizes.values(), default=0),
        "images_without_point_tracks": len(set(image_ids).difference(images_with_tracks)),
    }


def _unreferenced_entity_summary(model: ColmapModelData) -> dict[str, int]:
    """중복 frame 대표화 뒤에도 보존한 미참조 camera·rig 개수를 집계합니다."""

    referenced_camera_ids = {
        image.camera_id
        for image in model.images.image_records.values()
    }
    referenced_rig_ids = {frame.rig_id for frame in model.frames.values()}
    return {
        "unreferenced_cameras": len(set(model.cameras).difference(referenced_camera_ids)),
        "unreferenced_rigs": len(set(model.rigs).difference(referenced_rig_ids)),
    }


def _reprojection_summary(model: ColmapModelData) -> dict[str, float | int | str | None]:
    """engine이 다시 계산한 output Point3D 평균 reprojection error 분포를 요약합니다."""

    errors = model.points.errors
    if errors.size == 0:
        return {
            "point_count": 0,
            "mean_pixels": None,
            "maximum_pixels": None,
            "reason": "2관측 미만 track drop 뒤 남은 Point3D가 없습니다.",
        }
    return {
        "point_count": model.points.count,
        "mean_pixels": float(np.mean(errors)),
        "maximum_pixels": float(np.max(errors)),
    }


def _addon_version() -> str | None:
    """설치된 애드온 metadata에서 버전을 읽고 불가능하면 report에 null을 남깁니다."""

    try:
        import align

        version = align.bl_info.get("version")
        if isinstance(version, tuple) and all(isinstance(item, int) for item in version):
            return ".".join(str(item) for item in version)
    except Exception:
        return None
    return None


def _report_mapping(
    snapshot: MergeExportSnapshot,
    audits: Sequence[SourceAudit],
    raw_sources: Sequence[LoadedMergeSource],
    engine_result: MergeEngineResult,
    output_hashes: Mapping[str, str],
    image_bytes: int,
    id_map_hash: str,
    started_at: float,
    preflight_warnings: Sequence[MergeIssue],
    stage_timings_seconds: Mapping[str, float],
) -> dict[str, Any]:
    """대용량 ID map은 NPZ에 두고 JSON에는 정책·출처·요약·해시만 기록합니다."""

    source_audit_by_id = {audit.model_id: audit for audit in audits}
    addon_version = _addon_version()
    warnings = tuple(preflight_warnings) + tuple(engine_result.warnings)
    anchor_source = _anchor_source_snapshot(snapshot)
    source_reports: list[dict[str, Any]] = []
    for source in snapshot.source_models:
        alignment = Sim3Transform(matrix_float64=source.alignment_matrix_float64)
        output_transform = (
            compose_anchor_colmap_output_transform(
                alignment,
                source.display_matrix_float64,
                anchor_source.display_matrix_float64,
            )
            if snapshot.restore_import_axes
            else compose_project_world_transform(
                alignment,
                source.display_matrix_float64,
            )
        )
        source_reports.append(
            {
                **source_audit_by_id[source.model_id].to_mapping(),
                "display_name": source.display_name,
                "dataset_root": str(source.dataset_root),
                "geometry_kind": source.geometry_kind,
                "geometry_revision": source.geometry_revision,
                "is_anchor": source.is_anchor,
                "display_transform_fingerprint": source.display_fingerprint,
                "alignment_fingerprint": source.alignment_fingerprint,
                "baked_project_world_matrix": source.project_world_matrix_float64.tolist(),
                "baked_output_matrix": output_transform.matrix_float64.tolist(),
                "alignment_provenance": (
                    dict(source.alignment_provenance)
                    if source.alignment_provenance is not None
                    else {"method": "UNKNOWN"}
                ),
            }
        )
    return {
        "schema_version": 2,
        "export_job_id": snapshot.job_id,
        "addon_version": addon_version,
        "addon_version_reason": None if addon_version is not None else "worker 환경에서 애드온 metadata를 읽지 못했습니다.",
        "export_algorithm_version": snapshot.algorithm_version,
        "job_protocol_version": snapshot.protocol_version,
        "sparse_model_format": snapshot.output_format.value,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "output_contract": "modern COLMAP dataset with images/ and sparse/0; database.db is not included",
        "restore_import_axes": snapshot.restore_import_axes,
        "output_coordinate_basis": (
            "anchor_original_colmap_axes"
            if snapshot.restore_import_axes
            else "project_world"
        ),
        "source_models": source_reports,
        "excluded_model_ids": list(snapshot.excluded_model_ids),
        "excluded_geometry_kinds": {
            model_id: "TRIANGLE_MESH" for model_id in snapshot.excluded_model_ids
        },
        "merge_policy": snapshot.merge_policy.to_mapping(),
        "input_counts": _input_counts(raw_sources),
        "output_counts": _counts(engine_result.model),
        "image_copy_summary": {
            "image_files": engine_result.model.images.count,
            "image_bytes": image_bytes,
        },
        "duplicate_image_groups": engine_result.duplicate_image_group_count,
        "merged_point_components": engine_result.merged_point_component_count,
        "point2d_merge_policy": "source_order_offset",
        "point3d_merge_policy": "global_id_without_cross_source_deduplication",
        "dropped_short_tracks": engine_result.dropped_short_track_count,
        "connected_component_summary": _connected_component_summary(engine_result.model),
        "unreferenced_entity_summary": _unreferenced_entity_summary(engine_result.model),
        "reprojection_summary": _reprojection_summary(engine_result.model),
        "warnings": [warning.to_mapping() for warning in warnings],
        "validation_results": {
            "source_fingerprint": "passed",
            "source_structure": "passed",
            "preflight": "passed",
            "output_structure": "passed",
            "output_images": "passed",
            "roundtrip": "passed" if snapshot.merge_policy.require_roundtrip_validation else "not_requested",
            "pycolmap": "not_requested",
            "colmap_cli": "not_requested",
        },
        "output_file_sha256": dict(sorted(output_hashes.items())),
        "id_map_file": _ID_MAP_NAME,
        "id_map_sha256": id_map_hash,
        "stage_timings_seconds": {
            **{
                key: max(0.0, float(value))
                for key, value in stage_timings_seconds.items()
            },
            "total": max(0.0, monotonic() - started_at),
        },
        "elapsed_seconds": max(0.0, monotonic() - started_at),
        "peak_memory_bytes": None,
        "peak_memory_bytes_reason": "worker 전체 RSS peak 측정 API는 현재 플랫폼 계약에 포함하지 않아 기록하지 않았습니다.",
    }


def _failure_issue(error: BaseException, *, cancelled: bool) -> MergeIssue:
    """예외를 UI가 바로 표시할 원인·영향·해결 구조로 바꿉니다."""

    if cancelled:
        return MergeIssue(
            code="EXPORT_CANCELLED",
            cause="사용자가 구조 통합 내보내기 작업을 취소했습니다.",
            impact="정상 출력 폴더는 만들지 않으며 candidate만 정리합니다.",
            remedy="필요하면 A 그룹과 출력 경로를 확인한 뒤 새 작업을 시작하십시오.",
        )
    return MergeIssue(
        code="EXPORT_WORKER_FAILED",
        cause=str(error) or error.__class__.__name__,
        impact="COLMAP 구조·관측 트랙의 무결성을 보장할 수 없어 정상 출력 폴더를 만들지 않습니다.",
        remedy="오류 원인에 표시된 source 구조·중복 이미지·출력 경로를 수정한 뒤 다시 시도하십시오.",
    )


def _write_terminal_result(
    files: WorkerJobFiles,
    result: MergeExportResult,
) -> None:
    """성공은 result.json, 실패·취소는 error.json으로 한 번만 기록합니다."""

    target = files.result_path if result.error is None else files.error_path
    _atomic_write_json(target, result.to_mapping())


def run_worker_job(job_directory: str | Path) -> MergeExportResult:
    """한 job manifest를 실행해 preflight 또는 candidate export 결과를 반환합니다."""

    files = WorkerJobFiles(_strict_job_directory(job_directory))
    snapshot, preflight_only = _load_manifest(files)
    started_at = monotonic()
    stage_timings_seconds: dict[str, float] = {}
    candidate_created = False
    try:
        _raise_if_cancelled(files)
        _emit_progress(files, snapshot, MergeStage.SNAPSHOT_INPUTS, 0.0, "내보내기 입력 snapshot을 확인했습니다.")
        stage_started_at = monotonic()
        raw_sources, audits = _fingerprint_and_load_sources(files, snapshot)
        stage_timings_seconds["source_fingerprint_and_load"] = monotonic() - stage_started_at
        _raise_if_cancelled(files)
        stage_started_at = monotonic()
        preflight = run_preflight(
            snapshot,
            loaded_sources=raw_sources,
            cancel_requested=lambda: files.cancel_path.exists(),
            progress=lambda progress: _write_preflight_progress(
                files,
                progress,
                preflight_only=preflight_only,
            ),
        )
        stage_timings_seconds["preflight"] = monotonic() - stage_started_at
        _atomic_write_json(files.preflight_path, preflight.to_mapping())
        if not preflight.is_ready:
            issue = preflight.errors[0]
            result = MergeExportResult(
                job_id=snapshot.job_id,
                phase=MergePhase.FAILED,
                runtime_fingerprint=snapshot.runtime_fingerprint,
                candidate_root=snapshot.candidate_root,
                output_root=snapshot.output_root,
                warnings=preflight.warnings,
                error=issue,
            )
            _emit_progress(files, snapshot, MergeStage.FAILED, 1.0, "사전 검사 오류로 내보내기를 중단했습니다.", force=True)
            _write_terminal_result(files, result)
            return result
        if preflight_only:
            result = MergeExportResult(
                job_id=snapshot.job_id,
                phase=MergePhase.PREFLIGHT_READY,
                runtime_fingerprint=snapshot.runtime_fingerprint,
                candidate_root=snapshot.candidate_root,
                output_root=snapshot.output_root,
                counts={"source_models": len(raw_sources), "duplicate_image_groups": preflight.duplicate_image_group_count},
                warnings=preflight.warnings,
            )
            _emit_progress(files, snapshot, MergeStage.COMPLETED, 1.0, "사전 검사가 완료되었습니다.", force=True)
            _write_terminal_result(files, result)
            return result
        _raise_if_cancelled(files)
        stage_started_at = monotonic()
        _emit_progress(files, snapshot, MergeStage.NORMALIZE_RIGS_AND_FRAMES, 0.52, "legacy 모델을 modern rig/frame 구조로 정규화하고 있습니다.")
        anchor_source = _anchor_source_snapshot(snapshot)
        baked_sources = tuple(
            _bake_source(
                source,
                anchor_source.display_matrix_float64,
                restore_import_axes=snapshot.restore_import_axes,
            )
            for source in raw_sources
        )
        _raise_if_cancelled(files)
        bake_message = (
            "확정 Alignment를 적용하고 import 축변환을 Anchor의 원래 COLMAP 축으로 되돌렸습니다."
            if snapshot.restore_import_axes
            else "확정 Alignment와 Display Axis를 Project World에 적용했습니다."
        )
        _emit_progress(
            files,
            snapshot,
            MergeStage.BAKE_PROJECT_WORLD,
            0.60,
            bake_message,
        )
        stage_timings_seconds["normalize_and_bake"] = monotonic() - stage_started_at
        candidate = _create_candidate(snapshot)
        candidate_created = True
        _emit_progress(files, snapshot, MergeStage.MERGE_IMAGES_BY_BASENAME, 0.68, "basename 기준 이미지와 전역 ID를 통합하고 있습니다.")
        stage_started_at = monotonic()
        engine_result = merge_baked_models(
            baked_sources,
            output_source_path=candidate / "sparse" / "0",
            output_model_id=f"merged_{snapshot.job_id}",
            cancel_requested=lambda: files.cancel_path.exists(),
            progress=lambda stage, fraction, message, counts: _emit_progress(
                files,
                snapshot,
                stage,
                fraction,
                message,
                counts=counts,
            ),
        )
        stage_timings_seconds["merge"] = monotonic() - stage_started_at
        _raise_if_cancelled(files)
        output_directory = candidate / "sparse" / "0"
        stage_started_at = monotonic()
        if snapshot.output_format is MergeOutputFormat.TEXT:
            _emit_progress(files, snapshot, MergeStage.WRITE_TEXT_MODEL, 0.81, "modern COLMAP text 5파일을 candidate에 기록하고 있습니다.", counts=_counts(engine_result.model))
            output_paths = write_text_model(engine_result.model, output_directory)
        else:
            _emit_progress(files, snapshot, MergeStage.WRITE_BINARY_MODEL, 0.81, "modern COLMAP binary 5파일을 candidate에 기록하고 있습니다.", counts=_counts(engine_result.model))
            output_paths = write_binary_model(engine_result.model, output_directory)
        stage_timings_seconds["write_sparse"] = monotonic() - stage_started_at
        _raise_if_cancelled(files)
        _emit_progress(files, snapshot, MergeStage.ROUNDTRIP_VALIDATE, 0.84, "기록한 sparse model을 다시 읽어 검증하고 있습니다.", counts=_counts(engine_result.model))
        stage_started_at = monotonic()
        if snapshot.merge_policy.require_roundtrip_validation:
            _roundtrip_validate(candidate, engine_result.model, snapshot.output_format)
        stage_timings_seconds["roundtrip_validate"] = monotonic() - stage_started_at
        _raise_if_cancelled(files)
        _emit_progress(
            files,
            snapshot,
            MergeStage.VALIDATE_SOURCE_IMAGES,
            0.86,
            "output Image.name과 대표 원본 이미지 파일을 연결하고 있습니다.",
            counts={"images": engine_result.model.images.count},
        )
        image_copy_plan = _build_image_copy_plan(candidate, raw_sources, engine_result)
        stage_started_at = monotonic()
        image_hashes, image_bytes = _copy_dataset_images(
            files,
            snapshot,
            candidate,
            image_copy_plan,
        )
        stage_timings_seconds["copy_and_hash_images"] = monotonic() - stage_started_at
        _raise_if_cancelled(files)
        _emit_progress(files, snapshot, MergeStage.WRITE_AUDIT_REPORTS, 0.98, "ID mapping과 병합 보고서를 기록하고 있습니다.", counts=_counts(engine_result.model))
        stage_started_at = monotonic()
        id_map_path = candidate / _ID_MAP_NAME
        _atomic_write_npz(id_map_path, engine_result.id_maps.to_npz_arrays())
        cancel_probe = lambda: _raise_if_cancelled(files)
        id_map_hash = _sha256_file(id_map_path, cancel_requested=cancel_probe)
        output_hashes = {
            f"sparse/0/{name}": _sha256_file(path, cancel_requested=cancel_probe)
            for name, path in output_paths.items()
        }
        output_hashes.update(image_hashes)
        stage_timings_seconds["write_audit_artifacts"] = monotonic() - stage_started_at
        report = _report_mapping(
            snapshot,
            audits,
            raw_sources,
            engine_result,
            output_hashes,
            image_bytes,
            id_map_hash,
            started_at,
            preflight.warnings,
            stage_timings_seconds,
        )
        _atomic_write_json(candidate / _REPORT_NAME, report)
        _raise_if_cancelled(files)
        result = MergeExportResult(
            job_id=snapshot.job_id,
            phase=MergePhase.WAITING_FOR_FINALIZE,
            runtime_fingerprint=snapshot.runtime_fingerprint,
            candidate_root=candidate,
            output_root=snapshot.output_root,
            counts={
                **_counts(engine_result.model),
                "duplicate_image_groups": engine_result.duplicate_image_group_count,
                "merged_point_components": engine_result.merged_point_component_count,
                "dropped_short_tracks": engine_result.dropped_short_track_count,
                "image_files": len(image_copy_plan),
                "image_bytes": image_bytes,
            },
            warnings=preflight.warnings,
        )
        _emit_progress(files, snapshot, MergeStage.WAITING_FOR_FINALIZE, 1.0, "worker 검증을 통과해 메인 Blender의 원자 확정을 기다리고 있습니다.", counts=result.counts, force=True)
        _write_terminal_result(files, result)
        return result
    except MergeCancelledError as error:
        if candidate_created:
            _cleanup_candidate(snapshot)
        issue = _failure_issue(error, cancelled=True)
        result = MergeExportResult(
            job_id=snapshot.job_id,
            phase=MergePhase.CANCELLED,
            runtime_fingerprint=snapshot.runtime_fingerprint,
            candidate_root=snapshot.candidate_root,
            output_root=snapshot.output_root,
            error=issue,
        )
        _emit_progress(files, snapshot, MergeStage.CANCELLED, 1.0, "구조 통합 내보내기를 취소하고 candidate를 정리했습니다.", force=True)
        _write_terminal_result(files, result)
        return result
    except BaseException as error:
        if candidate_created:
            _cleanup_candidate(snapshot)
        issue = _failure_issue(error, cancelled=False)
        result = MergeExportResult(
            job_id=snapshot.job_id,
            phase=MergePhase.FAILED,
            runtime_fingerprint=snapshot.runtime_fingerprint,
            candidate_root=snapshot.candidate_root,
            output_root=snapshot.output_root,
            error=issue,
        )
        _emit_progress(files, snapshot, MergeStage.FAILED, 1.0, "구조 통합 내보내기가 실패해 candidate를 정리했습니다.", force=True)
        _write_terminal_result(files, result)
        return result


def _parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Blender `--` 뒤 worker의 유일한 job-dir 인자를 엄격히 읽습니다."""

    parser = argparse.ArgumentParser(description="COLMAP 구조 통합 내보내기 worker")
    parser.add_argument("--job-dir", required=True)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """명령행 worker를 실행하고 구조화된 결과 파일로만 상태를 전달합니다."""

    parsed = _parse_arguments(arguments)
    run_worker_job(parsed.job_dir)
    return 0


if __name__ == "__main__":
    command_arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    raise SystemExit(main(command_arguments))


__all__ = (
    "SourceAudit",
    "WorkerJobFiles",
    "main",
    "run_worker_job",
)
