"""COLMAP 구조 통합 전에 source와 basename 전제를 엄격히 검사합니다."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re

import numpy as np

from ..colmap_io import ColmapModelData, load_sparse_model, validate_model
from .types import (
    MergeCancelledError,
    MergeExportPreflightResult,
    MergeExportProgress,
    MergeExportSnapshot,
    MergeIssue,
    MergePolicy,
    MergePreflightError,
    MergeStage,
    MergeSourceModelSnapshot,
)
from .transforms import compose_cam_from_world, quaternion_to_rotation_matrix


_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:(?:/|$)")
_POSE_ATOL = 1.0e-8


@dataclass(frozen=True, slots=True)
class LoadedMergeSource:
    """worker가 다시 읽은 모델과 immutable A 그룹 snapshot을 함께 보관합니다."""

    ordinal: int
    snapshot: MergeSourceModelSnapshot
    model: ColmapModelData

    def __post_init__(self) -> None:
        """source ordinal과 snapshot·모델 ID가 동일한지 검증합니다."""

        if self.ordinal < 0:
            raise ValueError("source ordinal은 0 이상이어야 합니다.")
        if self.snapshot.model_id != self.model.model_id:
            raise ValueError("적재 모델 ID가 내보내기 snapshot과 다릅니다.")


@dataclass(frozen=True, slots=True)
class DuplicateImageGroup:
    """같은 단순 파일명으로 통합 후보가 된 source 이미지 묶음입니다."""

    canonical_name: str
    members: tuple[tuple[LoadedMergeSource, int], ...]


def canonical_image_name(image_name: str) -> str:
    """경로와 무관하게 중복 판정에만 쓸 엄격한 단순 파일명을 반환합니다."""

    normalized = str(image_name).replace("\\", "/")
    if not normalized or normalized.startswith("/") or _WINDOWS_DRIVE_PATH.match(normalized):
        raise MergePreflightError("이미지 이름이 상대 경로가 아니어서 단순 파일명으로 통합할 수 없습니다.")
    path = PurePosixPath(normalized)
    if not path.parts or path.name in {"", ".", ".."} or ".." in path.parts:
        raise MergePreflightError("이미지 이름에 허용하지 않는 경로 이탈 요소가 있습니다.")
    return path.name.casefold()


def normalized_relative_image_name(image_name: str) -> str:
    """대표 이미지에 보존할 상대 경로를 `/` 구분자로 정규화합니다."""

    normalized = str(image_name).replace("\\", "/")
    canonical_image_name(normalized)
    return normalized


def source_image_root(source: MergeSourceModelSnapshot) -> Path:
    """snapshot의 Dataset Root 바로 아래 `images` 폴더를 안전하게 반환합니다."""

    dataset_root = Path(source.dataset_root).expanduser().resolve(strict=False)
    image_root = (dataset_root / "images").resolve(strict=False)
    if image_root.parent != dataset_root:
        raise MergePreflightError("이미지 루트가 Dataset Root 바로 아래에 있지 않습니다.")
    if not image_root.is_dir() or image_root.is_symlink():
        raise MergePreflightError(f"COLMAP images 폴더가 없거나 안전한 일반 폴더가 아닙니다: {image_root}")
    if hasattr(os.path, "isjunction") and os.path.isjunction(image_root):
        raise MergePreflightError(f"COLMAP images 폴더는 junction일 수 없습니다: {image_root}")
    return image_root


def source_image_path(source: MergeSourceModelSnapshot, image_name: str) -> Path:
    """COLMAP Image.name을 Dataset Root의 실제 일반 이미지 파일로 해석합니다."""

    relative_name = normalized_relative_image_name(image_name)
    relative_path = Path(*PurePosixPath(relative_name).parts)
    image_root = source_image_root(source)
    candidate = (image_root / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(image_root)
    except ValueError as error:
        raise MergePreflightError("COLMAP 이미지 경로가 images 폴더 밖을 가리킵니다.") from error
    if not candidate.is_file() or candidate.is_symlink():
        raise MergePreflightError(f"COLMAP 이미지 파일이 없거나 안전한 일반 파일이 아닙니다: {candidate}")
    return candidate


def _source_image_issues(source: LoadedMergeSource) -> tuple[MergeIssue, ...]:
    """한 source의 모든 Image.name이 실제 images 파일로 해석되는지 검사합니다."""

    issues: list[MergeIssue] = []
    try:
        source_image_root(source.snapshot)
    except MergePreflightError as error:
        return (
            _issue(
                "SOURCE_IMAGES_ROOT_INVALID",
                f"{source.snapshot.display_name}의 images 폴더를 사용할 수 없습니다: {error}",
                "sparse model과 함께 열 수 있는 완전한 COLMAP 데이터셋을 만들 수 없습니다.",
                "Dataset Root 바로 아래에 실제 images 폴더를 복구한 뒤 다시 시도하십시오.",
                model_id=source.snapshot.model_id,
            ),
        )
    for image_id in sorted(source.model.images.image_records):
        image = source.model.images.image_records[image_id]
        try:
            source_image_path(source.snapshot, image.name)
        except MergePreflightError as error:
            issues.append(
                _issue(
                    "SOURCE_IMAGE_FILE_INVALID",
                    f"{source.snapshot.display_name} IMAGE_ID {image_id}의 파일을 사용할 수 없습니다: {error}",
                    "출력 images 폴더와 sparse Image.name의 대응을 보장할 수 없습니다.",
                    "누락된 이미지 파일 또는 Image.name 상대 경로를 복구하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
            break
    return tuple(issues)


def _image_file_sha256(path: Path, cancel_requested: Callable[[], bool] | None) -> str:
    """중복 basename 후보 파일을 취소 가능하게 해시합니다."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _raise_if_cancelled(cancel_requested)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _issue(
    code: str,
    cause: str,
    impact: str,
    remedy: str,
    *,
    model_id: str | None = None,
) -> MergeIssue:
    """원인·영향·해결을 빠뜨리지 않은 사용자용 진단을 만듭니다."""

    return MergeIssue(code=code, cause=cause, impact=impact, remedy=remedy, model_id=model_id)


def _raise_if_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    """긴 preflight 경계에서 cancel sentinel을 즉시 반영합니다."""

    if cancel_requested is not None and cancel_requested():
        raise MergeCancelledError("사용자가 구조 통합 내보내기 작업을 취소했습니다.")


def _emit_progress(
    callback: Callable[[MergeExportProgress], None] | None,
    snapshot: MergeExportSnapshot,
    fraction: float,
    message: str,
    *,
    counts: Mapping[str, int] | None = None,
) -> None:
    """순수 preflight가 worker/UI에 작은 진행률만 전달합니다."""

    if callback is None:
        return
    callback(
        MergeExportProgress(
            job_id=snapshot.job_id,
            stage=MergeStage.PREFLIGHT_DUPLICATE_IMAGES,
            fraction=fraction,
            message=message,
            counts=counts or {},
        )
    )


def _load_source_models(
    snapshot: MergeExportSnapshot,
    *,
    cancel_requested: Callable[[], bool] | None,
    progress: Callable[[MergeExportProgress], None] | None,
) -> tuple[LoadedMergeSource, ...]:
    """worker가 source sparse 파일을 다시 읽어 main 메모리 사본을 사용하지 않게 합니다."""

    sources: list[LoadedMergeSource] = []
    total = len(snapshot.source_models)
    for ordinal, source in enumerate(snapshot.source_models):
        _raise_if_cancelled(cancel_requested)
        _emit_progress(
            progress,
            snapshot,
            0.05 + 0.30 * (ordinal / total),
            "COLMAP source를 다시 읽고 있습니다.",
            counts={"source_models": ordinal, "total_source_models": total},
        )
        model = load_sparse_model(
            source.source_path,
            input_format=source.source_format.upper(),
            model_id=source.model_id,
            display_name=source.display_name,
            geometry_revision=source.geometry_revision,
        )
        sources.append(LoadedMergeSource(ordinal=ordinal, snapshot=source, model=model))
    return tuple(sources)


def load_sources_for_snapshot(
    snapshot: MergeExportSnapshot,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    progress: Callable[[MergeExportProgress], None] | None = None,
) -> tuple[LoadedMergeSource, ...]:
    """worker가 호출할 source 재적재 공개 경계를 제공합니다."""

    return _load_source_models(
        snapshot,
        cancel_requested=cancel_requested,
        progress=progress,
    )


def _matrix_as_proper_sim3(matrix: np.ndarray) -> bool:
    """입력 4×4가 positive uniform scale과 proper rotation만 갖는지 확인합니다."""

    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        return False
    if not np.allclose(matrix[3], np.asarray((0.0, 0.0, 0.0, 1.0)), rtol=0.0, atol=1.0e-12):
        return False
    linear = matrix[:3, :3]
    scales = np.linalg.norm(linear, axis=0)
    if np.any(scales <= 1.0e-12) or not np.allclose(scales, scales[0], rtol=1.0e-10, atol=1.0e-12):
        return False
    rotation = linear / scales[0]
    return bool(
        np.allclose(rotation.T @ rotation, np.identity(3), rtol=0.0, atol=1.0e-10)
        and np.isclose(np.linalg.det(rotation), 1.0, rtol=0.0, atol=1.0e-10)
    )


def _quaternion_to_rotation(quaternion: np.ndarray) -> np.ndarray:
    """Hamilton `(qw,qx,qy,qz)` 쿼터니언을 회전행렬로 바꿉니다."""

    value = np.asarray(quaternion, dtype=np.float64)
    norm = float(np.linalg.norm(value))
    if not np.isfinite(norm) or norm <= 1.0e-12:
        raise MergePreflightError("이미지 자세 쿼터니언이 회전으로 정규화되지 않습니다.")
    qw, qx, qy, qz = value / norm
    return np.asarray(
        (
            (1.0 - 2.0 * (qy * qy + qz * qz), 2.0 * (qx * qy - qz * qw), 2.0 * (qx * qz + qy * qw)),
            (2.0 * (qx * qy + qz * qw), 1.0 - 2.0 * (qx * qx + qz * qz), 2.0 * (qy * qz - qx * qw)),
            (2.0 * (qx * qz - qy * qw), 2.0 * (qy * qz + qx * qw), 1.0 - 2.0 * (qx * qx + qy * qy)),
        ),
        dtype=np.float64,
    )


def _baked_pose_center_rotation(source: LoadedMergeSource, image_id: int) -> tuple[np.ndarray, np.ndarray]:
    """중복 pose 비교용으로 Alignment × Display Axis가 적용된 center·회전을 계산합니다."""

    image = source.model.images.image_records[image_id]
    pose = np.asarray(image.cam_from_world, dtype=np.float64)
    world_to_camera = _quaternion_to_rotation(pose[:4])
    center = -world_to_camera.T @ pose[4:]
    matrix = source.snapshot.project_world_matrix_float64
    if not _matrix_as_proper_sim3(matrix):
        raise MergePreflightError("확정 Alignment × Display Axis가 proper Sim(3)이 아닙니다.")
    linear = matrix[:3, :3]
    scale = float(np.linalg.norm(linear[:, 0]))
    project_rotation = linear / scale
    baked_center = linear @ center + matrix[:3, 3]
    baked_world_to_camera = world_to_camera @ project_rotation.T
    return baked_center, baked_world_to_camera


def _rotation_difference_degrees(first: np.ndarray, second: np.ndarray) -> float:
    """두 proper rotation의 최소 각도 차이를 degree로 반환합니다."""

    trace = float(np.trace(first @ second.T))
    cosine = float(np.clip((trace - 1.0) * 0.5, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _find_frame_for_image(model: ColmapModelData, image_id: int) -> tuple[int, bool] | None:
    """이미지의 frame과 non-trivial rig 여부를 반환합니다."""

    for frame_id, frame in model.frames.items():
        camera_data = tuple(data for data in frame.data_ids if data.sensor_type == "CAMERA")
        if any(data.data_id == image_id for data in camera_data):
            rig = model.rigs[frame.rig_id]
            is_nontrivial = len(camera_data) != 1 or len(rig.sensors) != 1
            return frame_id, is_nontrivial
    return None


def _source_contract_issues(source: LoadedMergeSource) -> tuple[MergeIssue, ...]:
    """worker bake 전에 지원 범위·POINT2D_IDX·modern pose 계약을 구조화해 검사합니다."""

    model = source.model
    issues: list[MergeIssue] = []
    if not _matrix_as_proper_sim3(source.snapshot.project_world_matrix_float64):
        issues.append(
            _issue(
                "INVALID_PROJECT_WORLD_TRANSFORM",
                f"{source.snapshot.display_name}의 Alignment × Display Axis가 positive uniform scale의 Sim(3)이 아닙니다.",
                "점·카메라·frame을 같은 Project World 좌표계로 안전하게 변환할 수 없습니다.",
                "확정 Alignment와 Display Axis 행렬의 회전·균일 scale·마지막 행을 확인하십시오.",
                model_id=source.snapshot.model_id,
            )
        )
    point2d_indices = model.points.track_point2d_indices
    uint32_maximum = int(np.iinfo(np.uint32).max)
    if point2d_indices.size and int(np.max(point2d_indices)) > uint32_maximum:
        issues.append(
            _issue(
                "POINT2D_INDEX_OUT_OF_RANGE",
                f"{source.snapshot.display_name}의 POINT2D_IDX가 uint32 범위를 벗어납니다.",
                "COLMAP binary track을 정확한 관측 인덱스로 기록할 수 없습니다.",
                "uint32 범위의 POINT2D_IDX를 가진 원본 COLMAP model을 사용하십시오.",
                model_id=source.snapshot.model_id,
            )
        )
    unsupported_sensor = any(
        sensor.sensor_type != "CAMERA"
        for rig in model.rigs.values()
        for sensor in rig.sensors
    ) or any(
        data.sensor_type != "CAMERA"
        for frame in model.frames.values()
        for data in frame.data_ids
    )
    if unsupported_sensor:
        issues.append(
            _issue(
                "UNSUPPORTED_NON_CAMERA_RIG_DATA",
                f"{source.snapshot.display_name}에 현재 내보내기가 보관하지 않는 IMU 또는 비-CAMERA rig/frame data가 있습니다.",
                "지원하지 않는 sensor payload를 조용히 삭제하면 COLMAP rig 의미가 손상됩니다.",
                "CAMERA data만 가진 모델을 내보내거나 해당 sensor payload 보존 기능을 추가한 뒤 다시 시도하십시오.",
                model_id=source.snapshot.model_id,
            )
        )
    if bool(model.rigs) != bool(model.frames):
        issues.append(
            _issue(
                "INCOMPLETE_MODERN_RIG_FRAME_STRUCTURE",
                f"{source.snapshot.display_name}의 rigs와 frames가 함께 존재하지 않습니다.",
                "legacy 정규화 대상인지 modern 구조인지 결정할 수 없어 카메라 자세 소유 관계가 모호합니다.",
                "legacy 3파일 모델처럼 rigs·frames를 모두 비우거나, 완전한 modern rigs·frames 5파일을 사용하십시오.",
                model_id=source.snapshot.model_id,
            )
        )
        return tuple(issues)
    if not model.rigs:
        return tuple(issues)
    for frame in model.frames.values():
        rig = model.rigs.get(frame.rig_id)
        if rig is None:
            continue
        sensors = {(sensor.sensor_type, sensor.sensor_id): sensor for sensor in rig.sensors}
        for data in frame.data_ids:
            if data.sensor_type != "CAMERA":
                continue
            sensor = sensors.get((data.sensor_type, data.sensor_id))
            image = model.images.image_records.get(data.data_id)
            if sensor is None or image is None:
                continue
            composed = compose_cam_from_world(sensor.sensor_from_rig, frame.rig_from_world)
            composed_rotation = quaternion_to_rotation_matrix(composed[:4])
            image_rotation = quaternion_to_rotation_matrix(image.cam_from_world[:4])
            if not (
                np.allclose(composed_rotation, image_rotation, rtol=0.0, atol=_POSE_ATOL)
                and np.allclose(composed[4:], image.cam_from_world[4:], rtol=0.0, atol=_POSE_ATOL)
            ):
                issues.append(
                    _issue(
                        "MODERN_RIG_FRAME_IMAGE_POSE_MISMATCH",
                        f"{source.snapshot.display_name}의 FRAME_ID {frame.frame_id} rig/frame pose와 IMAGE_ID {image.image_id} pose가 일치하지 않습니다.",
                        "Project World bake 뒤 카메라 자세와 rig/frame 관계를 동시에 신뢰할 수 없습니다.",
                        "원본 modern COLMAP model의 sensor_from_rig × rig_from_world 관계를 복구한 뒤 다시 내보내십시오.",
                        model_id=source.snapshot.model_id,
                    )
                )
                return tuple(issues)
    return tuple(issues)


def build_duplicate_image_groups(
    sources: Sequence[LoadedMergeSource],
) -> tuple[tuple[DuplicateImageGroup, ...], tuple[MergeIssue, ...]]:
    """source 내 basename 충돌과 source 간 중복 후보를 결정적으로 수집합니다."""

    grouped: dict[str, list[tuple[LoadedMergeSource, int]]] = defaultdict(list)
    issues: list[MergeIssue] = []
    for source in sources:
        names_in_source: dict[str, int] = {}
        for image_id in sorted(source.model.images.image_records):
            image = source.model.images.image_records[image_id]
            try:
                canonical = canonical_image_name(image.name)
            except MergePreflightError as error:
                issues.append(
                    _issue(
                        "INVALID_IMAGE_NAME",
                        f"{source.snapshot.display_name}의 IMAGE_ID {image_id} 이름이 안전한 상대 경로가 아닙니다: {error}",
                        "출력 COLMAP 이미지 이름과 basename 통합 기준을 확정할 수 없습니다.",
                        "COLMAP 이미지 이름을 drive·절대 경로·.. 없는 상대 경로로 다시 생성하십시오.",
                        model_id=source.snapshot.model_id,
                    )
                )
                continue
            previous = names_in_source.get(canonical)
            if previous is not None:
                issues.append(
                    _issue(
                        "DUPLICATE_BASENAME_WITHIN_SOURCE",
                        f"{source.snapshot.display_name} 안에 같은 단순 파일명 {canonical!r}의 IMAGE_ID {previous}, {image_id}가 있습니다.",
                        "같은 source 안에서 어느 이미지를 대표로 써야 할지 결정할 수 없어 트랙이 손상될 수 있습니다.",
                        "source COLMAP에서 basename이 고유하도록 이미지 경로 또는 입력 묶음을 정리하십시오.",
                        model_id=source.snapshot.model_id,
                    )
                )
            else:
                names_in_source[canonical] = image_id
            grouped[canonical].append((source, image_id))
    groups = tuple(
        DuplicateImageGroup(canonical_name=name, members=tuple(members))
        for name, members in sorted(grouped.items())
        if len(members) > 1
    )
    return groups, tuple(issues)


def _validate_duplicate_group(
    group: DuplicateImageGroup,
    policy: MergePolicy,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[MergeIssue, ...]:
    """같은 basename의 해상도를 검증하고 대표 camera·pose 차이를 기록합니다."""

    issues: list[MergeIssue] = []
    representative_source, representative_image_id = group.members[0]
    representative_image = representative_source.model.images.image_records[representative_image_id]
    representative_camera = representative_source.model.cameras[representative_image.camera_id]
    try:
        representative_file = source_image_path(
            representative_source.snapshot,
            representative_image.name,
        )
        representative_size = representative_file.stat().st_size
    except (MergePreflightError, OSError) as error:
        return (
            _issue(
                "DUPLICATE_IMAGE_FILE_INVALID",
                f"{group.canonical_name} 대표 이미지 파일을 검증할 수 없습니다: {error}",
                "동일 basename이 실제로 같은 이미지인지 확인할 수 없습니다.",
                "대표 source의 images 파일을 복구한 뒤 다시 시도하십시오.",
                model_id=representative_source.snapshot.model_id,
            ),
        )
    representative_hash: str | None = None
    try:
        representative_center, representative_rotation = _baked_pose_center_rotation(
            representative_source,
            representative_image_id,
        )
    except MergePreflightError as error:
        return (
            _issue(
                "INVALID_PROJECT_WORLD_TRANSFORM",
                f"{representative_source.snapshot.display_name}의 확정 변환을 중복 pose 검사에 적용할 수 없습니다: {error}",
                "Project World 좌표와 카메라 자세를 안전하게 기록할 수 없습니다.",
                "확정 Alignment와 Display Axis가 positive uniform scale의 Sim(3)인지 확인하십시오.",
                model_id=representative_source.snapshot.model_id,
            ),
        )
    for source, image_id in group.members[1:]:
        image = source.model.images.image_records[image_id]
        try:
            image_file = source_image_path(source.snapshot, image.name)
            image_size = image_file.stat().st_size
            if image_size == representative_size:
                if representative_hash is None:
                    representative_hash = _image_file_sha256(
                        representative_file,
                        cancel_requested,
                    )
                same_bytes = (
                    _image_file_sha256(image_file, cancel_requested)
                    == representative_hash
                )
            else:
                # 크기가 다르면 이미 내용 차이가 확정되므로 대용량 파일 전체 해시를 생략합니다.
                same_bytes = False
        except (MergePreflightError, OSError) as error:
            issues.append(
                _issue(
                    "DUPLICATE_IMAGE_FILE_INVALID",
                    f"{group.canonical_name} 이미지 파일을 검증할 수 없습니다: {error}",
                    "동일 basename이 실제로 같은 이미지인지 확인할 수 없습니다.",
                    "해당 source의 images 파일을 복구한 뒤 다시 시도하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
            continue
        if not same_bytes:
            issues.append(
                _issue(
                    "DUPLICATE_IMAGE_FILE_REENCODED",
                    f"{group.canonical_name}의 이미지 파일 bytes가 source마다 다릅니다.",
                    "동일 basename과 호환되는 COLMAP 기하를 기준으로 Anchor 대표 파일 하나만 출력합니다.",
                    "동일 촬영 이미지의 재인코딩 차이라면 조치가 필요 없으며, 다른 촬영 이미지라면 basename을 분리하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
        camera = source.model.cameras[image.camera_id]
        if camera.width != representative_camera.width or camera.height != representative_camera.height:
            issues.append(
                _issue(
                    "DUPLICATE_IMAGE_RESOLUTION_MISMATCH",
                    f"{group.canonical_name}의 이미지 크기가 대표 이미지와 다릅니다.",
                    "서로 다른 픽셀 좌표계를 한 output 이미지의 POINTS2D 배열로 합칠 수 없습니다.",
                    "동일 해상도의 원본 이미지를 사용하거나 basename이 겹치지 않게 source를 분리하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
        if (
            camera.model_name != representative_camera.model_name
            or camera.params_float64.shape != representative_camera.params_float64.shape
            or not np.allclose(
                camera.params_float64,
                representative_camera.params_float64,
                rtol=policy.duplicate_camera_param_rtol,
                atol=policy.duplicate_camera_param_atol,
            )
        ):
            issues.append(
                _issue(
                    "DUPLICATE_IMAGE_CAMERA_REPRESENTATIVE",
                    f"{group.canonical_name}의 camera 모델 또는 내부 파라미터가 대표 이미지와 달라 Anchor 우선 대표 camera를 사용합니다.",
                    "다른 source의 측정 POINTS2D는 유지되지만 대표 camera 기준 재투영 오차가 달라질 수 있습니다.",
                    "내보내기 보고서에서 경고를 확인하고 필요하면 통합 모델을 bundle adjustment로 정제하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
        try:
            center, rotation = _baked_pose_center_rotation(source, image_id)
        except MergePreflightError as error:
            issues.append(
                _issue(
                    "INVALID_PROJECT_WORLD_TRANSFORM",
                    f"{source.snapshot.display_name}의 확정 변환을 중복 pose 검사에 적용할 수 없습니다: {error}",
                    "Project World 좌표와 카메라 자세를 안전하게 기록할 수 없습니다.",
                    "확정 Alignment와 Display Axis가 positive uniform scale의 Sim(3)인지 확인하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
            continue
        rotation_error = _rotation_difference_degrees(rotation, representative_rotation)
        center_error = float(np.linalg.norm(center - representative_center))
        if (
            rotation_error > policy.duplicate_pose_max_rotation_degrees
            or center_error > policy.duplicate_pose_max_center_distance
        ):
            issues.append(
                _issue(
                    "DUPLICATE_IMAGE_POSE_REPRESENTATIVE",
                    f"{group.canonical_name}의 baked pose가 대표와 회전 {rotation_error:.12g}°, 중심 {center_error:.12g}만큼 달라 Anchor 우선 대표 pose를 사용합니다.",
                    "다른 source의 측정 POINTS2D와 3D 포인트는 유지되지만 대표 pose 기준 재투영 오차가 달라질 수 있습니다.",
                    "정렬 품질과 내보내기 보고서를 확인하고 필요하면 통합 모델을 bundle adjustment로 정제하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
    nontrivial_sources = [
        (source, image_id)
        for source, image_id in group.members
        if (frame := _find_frame_for_image(source.model, image_id)) is not None and frame[1]
    ]
    if nontrivial_sources:
        source, image_id = nontrivial_sources[0]
        issues.append(
            _issue(
                "NONTRIVIAL_RIG_DUPLICATE_IMAGE_TOPOLOGY",
                f"{group.canonical_name}의 IMAGE_ID {image_id}가 multi-camera rig 또는 multi-image frame에 속합니다.",
                "한 output image를 정확히 한 frame에만 연결하는 규칙을 보장할 수 없어 rig 의미가 손상될 수 있습니다.",
                "중복 이미지를 제거한 source로 내보내거나 non-trivial rig topology를 분리해 주십시오.",
                model_id=source.snapshot.model_id,
            )
        )
    return tuple(issues)


def _snapshot_issues(snapshot: MergeExportSnapshot) -> tuple[MergeIssue, ...]:
    """파일 적재 전 source 경로·출력 경로와 provenance 감사 경고를 수집합니다."""

    issues: list[MergeIssue] = []
    output = snapshot.output_root.resolve(strict=False)
    if output.exists():
        if snapshot.overwrite_existing_output:
            issues.append(
                _issue(
                    "OUTPUT_ROOT_WILL_BE_REPLACED",
                    f"사용자가 기존 대상 폴더의 덮어쓰기를 승인했습니다: {output}",
                    "새 결과가 모든 검증을 통과한 뒤에만 기존 데이터셋을 교체합니다.",
                    "기존 결과를 유지하려면 worker 완료 전에 작업을 취소하십시오.",
                )
            )
        else:
            issues.append(
                _issue(
                    "OUTPUT_ROOT_ALREADY_EXISTS",
                    f"대상 폴더가 이미 존재합니다: {output}",
                    "사용자 승인 없이 기존 산출물을 덮어쓸 수 없어 내보내기를 시작하지 않습니다.",
                    "Export를 다시 누르고 덮어쓰기 확인 메시지에서 승인하십시오.",
                )
            )
    if not output.parent.is_dir():
        issues.append(
            _issue(
                "OUTPUT_PARENT_NOT_FOUND",
                f"대상 폴더의 부모 경로가 없습니다: {output.parent}",
                "원자 후보 폴더를 만들 수 없어 안전한 내보내기를 할 수 없습니다.",
                "존재하고 쓰기 가능한 부모 폴더 아래의 새 이름을 지정하십시오.",
            )
        )
    for source in snapshot.source_models:
        source_path = source.source_path.resolve(strict=False)
        dataset_root = source.dataset_root.resolve(strict=False)
        if not dataset_root.is_dir():
            issues.append(
                _issue(
                    "DATASET_ROOT_NOT_FOUND",
                    f"{source.display_name}의 Dataset Root가 없습니다: {dataset_root}",
                    "images와 sparse를 함께 검증해 완전한 출력 데이터셋을 만들 수 없습니다.",
                    "원본 Dataset Root를 복구하거나 모델을 다시 연결하십시오.",
                    model_id=source.model_id,
                )
            )
        if not source_path.exists():
            issues.append(
                _issue(
                    "SOURCE_PATH_NOT_FOUND",
                    f"{source.display_name}의 COLMAP source 경로가 없습니다: {source_path}",
                    "원본 sparse model을 다시 읽어 검증할 수 없습니다.",
                    "source 폴더를 복구하거나 해당 모델을 P 그룹으로 이동하십시오.",
                    model_id=source.model_id,
                )
            )
        try:
            output.relative_to(dataset_root)
            output_is_inside_source = True
        except ValueError:
            output_is_inside_source = False
        if output_is_inside_source:
            issues.append(
                _issue(
                    "OUTPUT_INSIDE_SOURCE",
                    f"출력 경로가 {source.display_name}의 원본 Dataset Root 내부입니다.",
                    "원본 sparse·images를 새 산출물로 오염시키거나 재귀 복사할 수 있습니다.",
                    "원본 Dataset Root 밖의 새 형제 또는 별도 폴더를 지정하십시오.",
                    model_id=source.model_id,
                )
            )
        if source.provenance_is_unknown:
            issues.append(
                _issue(
                    "UNKNOWN_ALIGNMENT_PROVENANCE",
                    f"{source.display_name}의 확정 정렬 provenance가 없습니다.",
                    "정렬 수치의 출처는 보고서에서 UNKNOWN으로 남지만 A 그룹 병합 대상에서는 제외하지 않습니다.",
                    "필요하면 Marker 또는 ICP 정렬을 다시 승인해 감사 정보를 갱신하십시오.",
                    model_id=source.model_id,
                )
            )
        elif str(source.alignment_provenance.get("method", "")).strip().upper() == "MANUAL":
            issues.append(
                _issue(
                    "MANUAL_ALIGNMENT_PROVENANCE",
                    f"{source.display_name}은 수동 A 그룹 편입으로 정렬되었습니다.",
                    "수치 품질 지표 없이 현재 확정 변환을 사용해 병합합니다.",
                    "필요하면 Marker 또는 ICP 결과를 검토한 뒤 다시 승인하십시오.",
                    model_id=source.model_id,
                )
            )
    return tuple(issues)


def _split_issues(issues: Sequence[MergeIssue]) -> tuple[tuple[MergeIssue, ...], tuple[MergeIssue, ...]]:
    """코드 접두 규칙으로 감사 경고와 기록 차단 오류를 나눕니다."""

    warning_codes = {
        "DUPLICATE_IMAGE_CAMERA_REPRESENTATIVE",
        "DUPLICATE_IMAGE_FILE_REENCODED",
        "DUPLICATE_IMAGE_POSE_REPRESENTATIVE",
        "UNKNOWN_ALIGNMENT_PROVENANCE",
        "MANUAL_ALIGNMENT_PROVENANCE",
        "OUTPUT_ROOT_WILL_BE_REPLACED",
    }
    warnings = tuple(issue for issue in issues if issue.code in warning_codes)
    errors = tuple(issue for issue in issues if issue.code not in warning_codes)
    return warnings, errors


def run_preflight(
    snapshot: MergeExportSnapshot,
    *,
    loaded_sources: Sequence[LoadedMergeSource] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    progress: Callable[[MergeExportProgress], None] | None = None,
) -> MergeExportPreflightResult:
    """worker 또는 테스트에서 source·duplicate image 전제를 검사해 결과를 반환합니다."""

    if not isinstance(snapshot, MergeExportSnapshot):
        raise TypeError("run_preflight에는 MergeExportSnapshot이 필요합니다.")
    _raise_if_cancelled(cancel_requested)
    _emit_progress(progress, snapshot, 0.0, "구조 통합 입력을 검사하고 있습니다.")
    snapshot_issues = list(_snapshot_issues(snapshot))
    if loaded_sources is None:
        try:
            sources = _load_source_models(
                snapshot,
                cancel_requested=cancel_requested,
                progress=progress,
            )
        except MergeCancelledError:
            raise
        except Exception as error:
            snapshot_issues.append(
                _issue(
                    "SOURCE_LOAD_FAILED",
                    f"COLMAP source를 다시 읽지 못했습니다: {error}",
                    "원본 구조를 검증하지 못하므로 안전한 구조 통합을 할 수 없습니다.",
                    "source 경로와 COLMAP binary/text 파일의 무결성을 확인하십시오.",
                )
            )
            sources = ()
    else:
        sources = tuple(loaded_sources)
        expected_ids = tuple(source.model_id for source in snapshot.source_models)
        actual_ids = tuple(source.snapshot.model_id for source in sources)
        if actual_ids != expected_ids:
            snapshot_issues.append(
                _issue(
                    "LOADED_SOURCE_SNAPSHOT_MISMATCH",
                    "worker가 적재한 source 순서 또는 모델 ID가 A 그룹 snapshot과 다릅니다.",
                    "ID 재매핑과 Anchor 우선 대표점 선택의 결정성이 보장되지 않습니다.",
                    "최신 A 그룹 snapshot으로 worker를 다시 시작하십시오.",
                )
            )
    for source in sources:
        _raise_if_cancelled(cancel_requested)
        try:
            validate_model(source.model)
        except Exception as error:
            snapshot_issues.append(
                _issue(
                    "SOURCE_MODEL_VALIDATION_FAILED",
                    f"{source.snapshot.display_name}의 COLMAP 구조 검증이 실패했습니다: {error}",
                    "손상된 camera·image·track 참조를 출력 모델에 안전하게 옮길 수 없습니다.",
                    "원본 COLMAP sparse model을 검증·복구한 뒤 다시 시도하십시오.",
                    model_id=source.snapshot.model_id,
                )
            )
        snapshot_issues.extend(_source_contract_issues(source))
        snapshot_issues.extend(_source_image_issues(source))
    groups, group_issues = build_duplicate_image_groups(sources)
    snapshot_issues.extend(group_issues)
    for index, group in enumerate(groups):
        _raise_if_cancelled(cancel_requested)
        snapshot_issues.extend(
            _validate_duplicate_group(
                group,
                snapshot.merge_policy,
                cancel_requested=cancel_requested,
            )
        )
        _emit_progress(
            progress,
            snapshot,
            0.40 + 0.55 * ((index + 1) / max(len(groups), 1)),
            "동일 basename 이미지의 대표 파일·카메라·자세를 검사하고 있습니다.",
            counts={"duplicate_image_groups": index + 1},
        )
    warnings, errors = _split_issues(snapshot_issues)
    _emit_progress(
        progress,
        snapshot,
        1.0,
        "구조 통합 사전 검사가 완료되었습니다.",
        counts={"duplicate_image_groups": len(groups), "errors": len(errors)},
    )
    return MergeExportPreflightResult(
        job_id=snapshot.job_id,
        runtime_fingerprint=snapshot.runtime_fingerprint,
        included_model_ids=tuple(source.model_id for source in snapshot.source_models),
        excluded_model_ids=snapshot.excluded_model_ids,
        duplicate_image_group_count=len(groups),
        warnings=warnings,
        errors=errors,
    )


def require_preflight_ready(result: MergeExportPreflightResult) -> None:
    """치명 preflight 오류를 첫 원인·영향·해결 문구와 함께 worker에서 중단합니다."""

    if result.is_ready:
        return
    issue = result.errors[0]
    raise MergePreflightError(
        f"원인: {issue.cause}\n영향: {issue.impact}\n해결: {issue.remedy}"
    )


__all__ = (
    "DuplicateImageGroup",
    "LoadedMergeSource",
    "build_duplicate_image_groups",
    "canonical_image_name",
    "load_sources_for_snapshot",
    "normalized_relative_image_name",
    "require_preflight_ready",
    "run_preflight",
    "source_image_path",
    "source_image_root",
)
