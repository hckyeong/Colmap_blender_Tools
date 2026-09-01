"""프레임별 Source Image 요청과 소유권 제한 LRU Cache를 관리합니다."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import bpy

from .ownership import (
    DATASET_REVISION_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_ID_KEY,
    RECORD_KIND_KEY,
)

ROLE_KEY = RECORD_KIND_KEY
IMAGE_ID_KEY = RECORD_ID_KEY
SOURCE_OWNED_ROLE = "SOURCE_OWNED"
SOURCE_BORROWED_ROLE = "SOURCE_BORROWED"


class FrameSourceError(RuntimeError):
    """Source Image 요청 또는 소유권 오류를 나타냅니다."""


def _pointer(value) -> int:
    """삭제된 Blender ID를 안전하게 0으로 변환합니다."""
    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class SourceRequestToken:
    """늦게 완료된 Source 요청의 Commit을 차단하는 불변 Token입니다."""

    session_epoch: int
    dataset_revision: int
    scene_pointer: int
    request_serial: int
    frame: int
    image_id: int


@dataclass(slots=True)
class SourceImageRecord:
    """Cache에 있는 Source Image와 소유권을 보관합니다."""

    image_id: int
    image: object
    image_pointer: int
    owned: bool
    pinned: bool = False

    def valid(self) -> bool:
        """Source Image Pointer가 아직 유효한지 확인합니다."""
        return _pointer(self.image) == self.image_pointer


class FrameSourceCache:
    """최신 요청만 Commit하는 IMAGE_ID 기반 LRU Cache입니다."""

    def __init__(
        self,
        *,
        scene,
        session_epoch: int,
        dataset_revision: int,
        max_images: int = 3,
        detach_callback: Callable[[object], None] | None = None,
    ):
        """Scene과 Session Revision에 고정된 빈 Cache를 만듭니다."""
        if scene is None or _pointer(scene) == 0:
            raise FrameSourceError("Source Cache에 사용할 Scene이 없습니다.")
        if max_images < 1:
            raise FrameSourceError("Source Image Cache 크기는 1 이상이어야 합니다.")
        self.scene = scene
        self.scene_pointer = _pointer(scene)
        self.session_epoch = int(session_epoch)
        self.dataset_revision = int(dataset_revision)
        self.max_images = min(int(max_images), 3)
        self.detach_callback = detach_callback
        self._records: OrderedDict[int, SourceImageRecord] = OrderedDict()
        self._serial = 0
        self._latest: SourceRequestToken | None = None
        self._closed = False

    def begin_request(self, *, frame: int, image_id: int) -> SourceRequestToken:
        """새 요청을 최신 요청으로 지정하고 Token을 반환합니다."""
        if self._closed:
            raise FrameSourceError("종료된 Source Cache에는 요청할 수 없습니다.")
        self._serial += 1
        token = SourceRequestToken(
            session_epoch=self.session_epoch,
            dataset_revision=self.dataset_revision,
            scene_pointer=self.scene_pointer,
            request_serial=self._serial,
            frame=int(frame),
            image_id=int(image_id),
        )
        self._latest = token
        return token

    def is_current(self, token: SourceRequestToken, *, current_frame: int | None = None) -> bool:
        """Token이 현재 Session과 최신 Frame 요청에 정확히 일치하는지 확인합니다."""
        if self._closed or token != self._latest:
            return False
        if (
            token.session_epoch != self.session_epoch
            or token.dataset_revision != self.dataset_revision
            or token.scene_pointer != self.scene_pointer
            or _pointer(self.scene) != self.scene_pointer
        ):
            return False
        if current_frame is not None and int(current_frame) != token.frame:
            return False
        return True

    def get(self, image_id: int):
        """IMAGE_ID에 해당하는 유효 Image를 LRU 최신 위치로 반환합니다."""
        record = self._records.get(int(image_id))
        if record is None:
            return None
        if not record.valid():
            self._records.pop(int(image_id), None)
            return None
        self._records.move_to_end(int(image_id))
        return record.image

    def add_borrowed(self, image_id: int, image) -> object:
        """사용자 소유 Image를 해제하지 않는 차용 항목으로 추가합니다."""
        if image is None or _pointer(image) == 0:
            raise FrameSourceError("차용할 Source Image가 없습니다.")
        self._insert(
            SourceImageRecord(
                image_id=int(image_id),
                image=image,
                image_pointer=_pointer(image),
                owned=False,
            )
        )
        return image

    def load_owned(self, token: SourceRequestToken, path: str | Path):
        """최신 요청의 파일을 애드온 전용 Image로 동기 로드합니다."""
        if not self.is_current(token):
            raise FrameSourceError("이미 취소된 Source 요청입니다.")
        cached = self.get(token.image_id)
        if cached is not None:
            return cached
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise FrameSourceError(f"Source Image 파일이 없습니다: {source_path}")
        image = bpy.data.images.load(str(source_path), check_existing=False)
        image[OWNER_KEY] = OWNER_TOKEN
        image[ROLE_KEY] = SOURCE_OWNED_ROLE
        image[IMAGE_ID_KEY] = str(token.image_id)
        image[DATASET_REVISION_KEY] = int(token.dataset_revision)
        if not self.is_current(token):
            self._release_owned(image)
            raise FrameSourceError("Source 로드 중 최신 Frame 요청이 변경되었습니다.")
        self._insert(
            SourceImageRecord(
                image_id=token.image_id,
                image=image,
                image_pointer=_pointer(image),
                owned=True,
            )
        )
        return image

    def commit(
        self,
        token: SourceRequestToken,
        image,
        *,
        current_frame: int,
        apply: Callable[[object, int], None],
    ) -> bool:
        """최신 Token과 Frame이 일치할 때만 Source를 Stencil에 연결합니다."""
        if not self.is_current(token, current_frame=current_frame):
            return False
        cached = self.get(token.image_id)
        if cached is not image or _pointer(image) == 0:
            return False
        apply(image, token.image_id)
        if not self.is_current(token, current_frame=current_frame):
            return False
        self.pin(token.image_id)
        return True

    def pin(self, image_id: int) -> None:
        """현재 Stencil Source 하나만 퇴거 불가 상태로 유지합니다."""
        selected = int(image_id)
        for key, record in self._records.items():
            record.pinned = key == selected
        if selected in self._records:
            self._records.move_to_end(selected)
        self._evict()

    def invalidate(self, *, session_epoch: int | None = None) -> int:
        """Pending 요청을 취소하고 Session Epoch를 증가시킵니다."""
        self._serial += 1
        self._latest = None
        self.session_epoch = (
            int(session_epoch)
            if session_epoch is not None
            else int(self.session_epoch) + 1
        )
        return self.session_epoch

    def close(self, *, preserve_pinned: bool = False) -> None:
        """Pending 요청을 취소하고 필요하면 현재 Stencil Source는 Blender 데이터로 보존합니다."""
        if self._closed:
            return
        self.invalidate()
        self._closed = True
        for record in tuple(self._records.values()):
            if preserve_pinned and record.pinned:
                continue
            if record.owned and record.valid():
                self._detach(record.image)
                self._release_owned(record.image)
        self._records.clear()

    def records(self) -> tuple[SourceImageRecord, ...]:
        """현재 LRU 순서의 Cache 항목 Snapshot을 반환합니다."""
        return tuple(self._records.values())

    def _insert(self, record: SourceImageRecord) -> None:
        """동일 IMAGE_ID 이전 항목을 정리하고 새 항목을 추가합니다."""
        previous = self._records.pop(record.image_id, None)
        if previous is not None and previous.image is not record.image and previous.owned:
            self._detach(previous.image)
            self._release_owned(previous.image)
        self._records[record.image_id] = record
        self._evict()

    def _evict(self) -> None:
        """Cache 상한을 넘는 가장 오래된 비고정 항목을 퇴거합니다."""
        while len(self._records) > self.max_images:
            victim_key = next(
                (key for key, record in self._records.items() if not record.pinned),
                None,
            )
            if victim_key is None:
                return
            victim = self._records.pop(victim_key)
            if victim.owned and victim.valid():
                self._detach(victim.image)
                self._release_owned(victim.image)

    def _detach(self, image) -> None:
        """애드온 Stencil 참조를 외부 Callback으로 먼저 해제합니다."""
        if self.detach_callback is not None:
            self.detach_callback(image)

    @staticmethod
    def _release_owned(image) -> None:
        """정확한 소유권과 사용자 수가 확인된 Source Image만 제거합니다."""
        try:
            owned = bool(
                _pointer(image)
                and image.get(OWNER_KEY) == OWNER_TOKEN
                and image.get(ROLE_KEY) == SOURCE_OWNED_ROLE
            )
            if not owned:
                return
            if image.users == 0:
                image.gl_free()
                image.buffers_free()
                bpy.data.images.remove(image)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            return


__all__ = [
    "FrameSourceCache",
    "FrameSourceError",
    "SOURCE_BORROWED_ROLE",
    "SOURCE_OWNED_ROLE",
    "SourceImageRecord",
    "SourceRequestToken",
]
