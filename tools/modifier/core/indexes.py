"""COLMAP Reconstruction의 편집용 역방향 관계 인덱스입니다."""

from __future__ import annotations

from dataclasses import dataclass

from .integrity import validate_reconstruction
from .model import Reconstruction, ValidatedReconstructionToken


@dataclass(frozen=True)
class RelationshipIndexes:
    point_to_images: dict[int, frozenset[int]]
    image_to_points: dict[int, frozenset[int]]
    camera_to_images: dict[int, frozenset[int]]
    image_to_frame: dict[int, int]


def _build_relationship_indexes(model: Reconstruction) -> RelationshipIndexes:
    """검증 경계를 통과한 모델의 결정론적인 편집 인덱스를 구성합니다."""
    point_to_images = {
        point_id: frozenset(track.image_id for track in point.track)
        for point_id, point in model.points3d.items()
    }
    image_to_points = {
        image_id: frozenset(
            point.point3d_id
            for point in image.points2d
            if point.point3d_id is not None
        )
        for image_id, image in model.images.items()
    }

    camera_sets: dict[int, set[int]] = {camera_id: set() for camera_id in model.cameras}
    for image in model.images.values():
        camera_sets[image.camera_id].add(image.image_id)

    image_to_frame: dict[int, int] = {}
    for frame in model.frames.values():
        for data_id in frame.data_ids:
            if data_id.sensor_type != 0:
                continue
            if data_id.data_id in image_to_frame:
                raise ValueError(
                    f"IMAGE_ID {data_id.data_id}가 여러 Frame에 연결되어 있습니다."
                )
            image_to_frame[data_id.data_id] = frame.frame_id

    return RelationshipIndexes(
        point_to_images=point_to_images,
        image_to_points=image_to_points,
        camera_to_images={
            camera_id: frozenset(image_ids)
            for camera_id, image_ids in camera_sets.items()
        },
        image_to_frame=image_to_frame,
    )


def build_relationship_indexes(model: Reconstruction) -> RelationshipIndexes:
    """모델을 한 번 검증한 뒤 결정론적인 편집 인덱스를 구성합니다."""
    validate_reconstruction(model)
    return _build_relationship_indexes(model)


def build_relationship_indexes_validated(
    model: Reconstruction,
    token: ValidatedReconstructionToken,
    *,
    dataset_uuid: str,
    source_fingerprint: str,
    validation_revision: int,
) -> RelationshipIndexes:
    """Loader가 검증한 정확한 객체와 source revision에서만 재검사를 생략합니다."""

    if token.reconstruction is not model:
        raise ValueError("검증 token의 Reconstruction 객체가 현재 모델과 다릅니다.")
    if token.dataset_uuid != dataset_uuid:
        raise ValueError("검증 token의 Dataset UUID가 현재 source와 다릅니다.")
    if token.source_fingerprint != source_fingerprint:
        raise ValueError("검증 token의 source fingerprint가 현재 source와 다릅니다.")
    if token.validation_revision != validation_revision:
        raise ValueError("검증 token의 validation revision이 현재 경계와 다릅니다.")
    for name in ("cameras", "images", "points3d", "rigs", "frames"):
        if getattr(token, name) is not getattr(model, name):
            raise ValueError(
                f"검증 token 이후 Reconstruction {name} mapping이 변경되었습니다."
            )
    return _build_relationship_indexes(model)
