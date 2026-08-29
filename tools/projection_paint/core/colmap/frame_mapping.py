"""불연속 IMAGE_ID를 Blender 연속 Frame에 양방향으로 매핑합니다."""

from __future__ import annotations

from typing import Iterable

from .errors import ColmapError
from .model import FrameMapping, ImagePoseRecord

DEFAULT_FRAME_MIN = -1_048_574
DEFAULT_FRAME_MAX = 1_048_574


def build_frame_mapping(
    images: Iterable[ImagePoseRecord],
    *,
    frame_start: int = 1,
    frame_min: int = DEFAULT_FRAME_MIN,
    frame_max: int = DEFAULT_FRAME_MAX,
) -> FrameMapping:
    """IMAGE_ID 오름차순을 frame_start부터 연속 Frame에 배치합니다."""
    if not isinstance(frame_start, int):
        raise ColmapError("frame_start는 정수여야 합니다.")
    if frame_min > frame_max:
        raise ColmapError("Frame 최소값이 최대값보다 큽니다.")
    image_ids = tuple(sorted(image.image_id for image in images))
    if not image_ids:
        raise ColmapError("Frame에 배치할 Image가 없습니다.")
    if len(set(image_ids)) != len(image_ids):
        raise ColmapError("Frame Mapping에 중복 IMAGE_ID가 있습니다.")
    frame_end = frame_start + len(image_ids) - 1
    if frame_start < frame_min or frame_end > frame_max:
        raise ColmapError(
            f"연속 Frame 범위가 허용 범위를 벗어났습니다: "
            f"{frame_start}..{frame_end}"
        )
    frame_to_image = {
        frame_start + index: image_id
        for index, image_id in enumerate(image_ids)
    }
    image_to_frame = {
        image_id: frame
        for frame, image_id in frame_to_image.items()
    }
    return FrameMapping(
        frame_start=frame_start,
        frame_end=frame_end,
        frame_to_image=frame_to_image,
        image_to_frame=image_to_frame,
    )
