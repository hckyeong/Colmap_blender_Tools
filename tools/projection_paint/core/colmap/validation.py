"""Camera/Image 레코드의 의미 무결성을 Scene 생성 전에 검증합니다."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Generator, Mapping

from .camera_models import model_by_id, pinhole_parameters
from .errors import ColmapError
from .io_common import consume_generator
from .model import CameraRecord, ImagePoseRecord, ParserLimits, Point3DRecord

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
QUATERNION_NORM_EPSILON = 1.0e-12


def normalized_image_name_key(image_name: str) -> str:
    """이미지 이름 충돌 검사용 Unicode·Separator 정규화 Key를 반환합니다."""
    return unicodedata.normalize("NFC", image_name.replace("\\", "/")).casefold()


def validate_camera_record(camera: CameraRecord, limits: ParserLimits) -> None:
    """Camera ID, Model, 해상도와 Parameter를 검증합니다."""
    if not 1 <= camera.camera_id <= UINT32_MAX:
        raise ColmapError(
            f"CAMERA_ID가 지원 범위를 벗어났습니다: {camera.camera_id}"
        )
    model = model_by_id(camera.model_id)
    if camera.model_name != model.name:
        raise ColmapError(
            f"CAMERA_ID {camera.camera_id}의 Model ID와 이름이 일치하지 않습니다."
        )
    if len(camera.params) != model.num_params:
        raise ColmapError(
            f"CAMERA_ID {camera.camera_id}의 Parameter 수가 잘못되었습니다: "
            f"{len(camera.params)} != {model.num_params}"
        )
    if not model.exact_blender_projection:
        raise ColmapError(
            f"정확한 Paint 투영을 지원하지 않는 Camera Model입니다: "
            f"CAMERA_ID {camera.camera_id} ({camera.model_name})"
        )
    if not 1 <= camera.width <= limits.max_camera_dimension:
        raise ColmapError(
            f"CAMERA_ID {camera.camera_id}의 Width가 유효하지 않습니다: "
            f"{camera.width}"
        )
    if not 1 <= camera.height <= limits.max_camera_dimension:
        raise ColmapError(
            f"CAMERA_ID {camera.camera_id}의 Height가 유효하지 않습니다: "
            f"{camera.height}"
        )
    if not all(math.isfinite(value) for value in camera.params):
        raise ColmapError(
            f"CAMERA_ID {camera.camera_id}의 Parameter에 NaN 또는 Inf가 있습니다."
        )
    fx, fy, _cx, _cy = pinhole_parameters(camera.model_name, camera.params)
    if fx <= 0.0 or fy <= 0.0:
        raise ColmapError(
            f"CAMERA_ID {camera.camera_id}의 Focal Length는 0보다 커야 합니다."
        )


def validate_image_record(image: ImagePoseRecord) -> None:
    """Image ID, Camera 참조, Pose와 이름의 기본 형식을 검증합니다."""
    if not 1 <= image.image_id <= UINT32_MAX:
        raise ColmapError(
            f"IMAGE_ID가 지원 범위를 벗어났습니다: {image.image_id}"
        )
    if not 1 <= image.camera_id <= UINT32_MAX:
        raise ColmapError(
            f"IMAGE_ID {image.image_id}의 CAMERA_ID가 지원 범위를 벗어났습니다: "
            f"{image.camera_id}"
        )
    values = (*image.qvec, *image.tvec)
    if not all(math.isfinite(value) for value in values):
        raise ColmapError(
            f"IMAGE_ID {image.image_id}의 Pose에 NaN 또는 Inf가 있습니다."
        )
    norm = math.sqrt(sum(value * value for value in image.qvec))
    if norm <= QUATERNION_NORM_EPSILON:
        raise ColmapError(
            f"IMAGE_ID {image.image_id}의 Quaternion Norm이 0에 가깝습니다."
        )
    if not image.image_name:
        raise ColmapError(f"IMAGE_ID {image.image_id}의 이미지 이름이 비어 있습니다.")


def validate_camera_image_records_steps(
    cameras: Mapping[int, CameraRecord],
    images: Mapping[int, ImagePoseRecord],
    limits: ParserLimits,
) -> Generator[tuple[int, int], None, None]:
    """Camera와 Image 무결성을 레코드 단위로 검증해 처리량을 전달합니다."""
    if not cameras:
        raise ColmapError("COLMAP Camera 레코드가 비어 있습니다.")
    if not images:
        raise ColmapError("COLMAP Image 레코드가 비어 있습니다.")
    if len(cameras) > limits.max_cameras:
        raise ColmapError(
            f"Camera 수가 안전 상한을 초과했습니다: {len(cameras)}"
        )
    if len(images) > limits.max_images:
        raise ColmapError(
            f"Image 수가 안전 상한을 초과했습니다: {len(images)}"
        )
    total = len(cameras) + len(images)
    completed = 0
    yield completed, total
    for key, camera in cameras.items():
        if key != camera.camera_id:
            raise ColmapError(
                f"Camera Mapping Key와 CAMERA_ID가 다릅니다: {key}"
            )
        validate_camera_record(camera, limits)
        completed += 1
        if completed % limits.cancel_check_interval == 0:
            yield completed, total
    name_owners: dict[str, int] = {}
    for key, image in images.items():
        if key != image.image_id:
            raise ColmapError(
                f"Image Mapping Key와 IMAGE_ID가 다릅니다: {key}"
            )
        validate_image_record(image)
        if len(image.image_name.encode("utf-8", errors="strict")) > limits.max_image_name_bytes:
            raise ColmapError(
                f"IMAGE_ID {image.image_id}의 이미지 이름이 안전 상한을 초과했습니다."
            )
        if image.camera_id not in cameras:
            raise ColmapError(
                f"IMAGE_ID {image.image_id}가 존재하지 않는 CAMERA_ID "
                f"{image.camera_id}를 참조합니다."
            )
        name_key = normalized_image_name_key(image.image_name)
        previous_id = name_owners.get(name_key)
        if previous_id is not None:
            raise ColmapError(
                f"정규화 후 이미지 이름이 충돌합니다: "
                f"IMAGE_ID {previous_id}, {image.image_id}"
            )
        name_owners[name_key] = image.image_id
        completed += 1
        if completed % limits.cancel_check_interval == 0:
            yield completed, total


def validate_camera_image_records(
    cameras: Mapping[int, CameraRecord],
    images: Mapping[int, ImagePoseRecord],
    limits: ParserLimits,
) -> None:
    """Camera와 Image 증분 무결성 검사를 동기 완료합니다."""

    consume_generator(
        validate_camera_image_records_steps(cameras, images, limits)
    )


def validate_point3d_records_steps(
    points3d: Mapping[int, Point3DRecord],
    limits: ParserLimits,
) -> Generator[tuple[int, int], None, None]:
    """참고 입력의 원본 검사 결과를 재사용하고 전체 입력은 기존 무결성을 검사합니다."""
    from .shared_reader import PointRecordMapping
    if isinstance(points3d, PointRecordMapping) and points3d.preview is not None:
        preview = points3d.preview
        if preview.source_point_count > limits.max_points3d or preview.source_track_count > limits.max_track_elements:
            raise ColmapError("원본 점·관측 트랙 수가 안전 상한을 초과했습니다.")
        yield len(points3d), len(points3d)
        return
    if len(points3d) > limits.max_points3d:
        raise ColmapError(
            f"Point3D 수가 안전 상한을 초과했습니다: {len(points3d)}"
        )
    total_track_elements = 0
    from .shared_reader import PointRecordMapping

    track_count = (
        int(points3d.table.track_offsets[-1])
        if isinstance(points3d, PointRecordMapping)
        else sum(len(point.track) for point in points3d.values())
    )
    total = len(points3d) + track_count
    completed = 0
    yield completed, total
    for key, point in points3d.items():
        if key != point.point3d_id:
            raise ColmapError(
                f"Point3D Mapping Key와 POINT3D_ID가 다릅니다: {key}"
            )
        if not 0 <= point.point3d_id <= UINT64_MAX:
            raise ColmapError(
                f"POINT3D_ID가 지원 범위를 벗어났습니다: {point.point3d_id}"
            )
        if not all(math.isfinite(value) for value in (*point.xyz, point.error)):
            raise ColmapError(
                f"POINT3D_ID {point.point3d_id}에 NaN 또는 Inf가 있습니다."
            )
        if point.error < 0.0:
            raise ColmapError(
                f"POINT3D_ID {point.point3d_id}의 ERROR가 음수입니다."
            )
        if any(component < 0 or component > 255 for component in point.rgb):
            raise ColmapError(
                f"POINT3D_ID {point.point3d_id}의 RGB 범위가 잘못되었습니다."
            )
        total_track_elements += len(point.track)
        if total_track_elements > limits.max_track_elements:
            raise ColmapError(
                "Point3D Track 수가 안전 상한을 초과했습니다: "
                f"{total_track_elements}"
            )
        completed += 1
        if completed % limits.cancel_check_interval == 0:
            yield completed, total
        for element in point.track:
            if not 1 <= element.image_id <= UINT32_MAX:
                raise ColmapError(
                    f"POINT3D_ID {point.point3d_id}의 Track IMAGE_ID가 "
                    f"지원 범위를 벗어났습니다: {element.image_id}"
                )
            if not 0 <= element.point2d_index <= UINT32_MAX:
                raise ColmapError(
                    f"POINT3D_ID {point.point3d_id}의 POINT2D_IDX가 "
                    f"지원 범위를 벗어났습니다: {element.point2d_index}"
                )
            completed += 1
            if completed % limits.cancel_check_interval == 0:
                yield completed, total


def validate_point3d_records(
    points3d: Mapping[int, Point3DRecord],
    limits: ParserLimits,
) -> None:
    """Point3D 증분 무결성 검사를 동기 완료합니다."""

    consume_generator(validate_point3d_records_steps(points3d, limits))


def normalized_quaternion(
    qvec: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """검증된 Quaternion을 단위 길이로 정규화합니다."""
    norm = math.sqrt(sum(value * value for value in qvec))
    if not math.isfinite(norm) or norm <= QUATERNION_NORM_EPSILON:
        raise ColmapError("정규화할 수 없는 Quaternion입니다.")
    qw, qx, qy, qz = qvec
    return qw / norm, qx / norm, qy / norm, qz / norm
