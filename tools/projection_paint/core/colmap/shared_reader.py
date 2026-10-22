"""공통 COLMAP 파서의 배열을 Painter 전용 불변 보기로 연결합니다."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from operator import index as integer_index
import numpy as np

try:
    from .....shared.colmap_io import ColmapIOError, SourceFingerprintJob, create_incremental_sparse_model_reader, discover_sparse_model, validate_model_structure
except ImportError:
    from shared.colmap_io import ColmapIOError, SourceFingerprintJob, create_incremental_sparse_model_reader, discover_sparse_model, validate_model_structure

from .errors import ColmapError
from .validation import validate_camera_image_records
from .io_common import check_cancel, identity_from_stat
from .model import CameraRecord, ImagePoseRecord, Point3DRecord, PointPayload, TrackElement


class PointTrackView(Sequence):
    """트랙 배열의 필요한 원소만 Painter 레코드로 변환합니다."""

    def __init__(self, table, start, end):
        """원본 트랙 배열과 점별 구간을 보관합니다."""
        self.table, self.start, self.end = table, start, end

    def __len__(self):
        """현재 점의 트랙 개수를 반환합니다."""
        return self.end - self.start

    def __eq__(self, other):
        """트랙을 대형 복사 없이 값 순서로 비교합니다."""
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    def __getitem__(self, index):
        """요청한 트랙 원소만 변환합니다."""
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        offset = self.start + index
        return TrackElement(int(self.table.track_image_ids[offset]), int(self.table.track_point2d_indices[offset]))


class PointRecordMapping(Mapping):
    """점 식별자와 값의 오름차순 순회를 보장하는 불변 배열 매핑입니다."""

    def __init__(self, table, *, preview=None):
        """원본 배열과 명시적인 참고 메타데이터를 복사 없이 보관합니다."""
        self.table = table
        self.preview = preview
        self.order = np.argsort(table.point_ids)
        self.sorted_ids = table.point_ids[self.order]
        self.order.setflags(write=False)
        self.sorted_ids.setflags(write=False)

    def __len__(self):
        """원본 점 개수를 반환합니다."""
        return len(self.table.point_ids)

    def __iter__(self):
        """정렬된 점 식별자를 순회합니다."""
        return (int(value) for value in self.sorted_ids)

    def __getitem__(self, key):
        """64비트 정수의 정밀도를 보존하여 지정 점을 조회합니다."""
        try:
            exact_key = integer_index(key)
        except TypeError as exc:
            raise KeyError(key) from exc
        if not 0 <= exact_key <= (1 << 64) - 1:
            raise KeyError(key)
        found = int(np.searchsorted(self.sorted_ids, np.uint64(exact_key)))
        if found >= len(self) or int(self.sorted_ids[found]) != key:
            raise KeyError(key)
        return self._record_at(int(self.order[found]))

    def _record_at(self, index):
        """원본 배열의 한 행만 기존 불변 점 레코드로 변환합니다."""
        table = self.table
        return Point3DRecord(
            int(table.point_ids[index]),
            tuple(float(value) for value in table.positions[index]),
            tuple(int(value) for value in table.colors[index]),
            float(table.errors[index]),
            PointTrackView(table, int(table.track_offsets[index]), int(table.track_offsets[index + 1])),
        )

    def items(self):
        """전체 순회에서는 이진 검색을 반복하지 않고 정렬된 행을 읽습니다."""
        for index in self.order:
            record = self._record_at(int(index))
            yield record.point3d_id, record

    def values(self):
        """점 식별자 오름차순으로 추가 컨테이너 없이 레코드를 변환합니다."""
        return (self._record_at(int(index)) for index in self.order)



class PointPayloadView(Sequence):
    """검증된 점 배열을 Blender 준비용 지연 레코드로 제공합니다."""

    def __init__(self, records):
        """검증된 점 보기와 정렬 순서를 보관합니다."""
        self.records = records

    def __len__(self):
        """준비할 점 개수를 반환합니다."""
        return len(self.records)

    def blender_buffers(self):
        """검증된 배열에서 좌표·색상·오차·식별자를 Blender 기록 형식으로 일괄 변환합니다."""
        table, order = self.records.table, self.records.order
        ids = table.point_ids[order]
        colors = np.ones((len(ids), 4), dtype=np.float32)
        colors[:, :3] = table.colors[order].astype(np.float32) / np.float32(255.0)
        return (
            table.positions[order].astype(np.float32).reshape(-1), colors.reshape(-1),
            table.errors[order].astype(np.float32), np.arange(len(ids), dtype=np.int32),
            (ids & np.uint64(0xFFFFFFFF)).astype(np.uint32).view(np.int32),
            (ids >> np.uint64(32)).astype(np.uint32).view(np.int32),
        )

    def __getitem__(self, index):
        """요청한 점의 페이로드만 생성합니다."""
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        table = self.records.table
        row = int(self.records.order[index])
        return PointPayload(int(table.point_ids[row]), tuple(float(v) for v in table.positions[row]), tuple(int(v) for v in table.colors[row]), float(table.errors[row]))


def shared_dataset_steps(layout, limits, cancel_check, *, point_limit=None):
    """전체 읽기 또는 명시적인 참고 표본으로 원본을 검증하고 불변 보기를 반환합니다."""
    try:
        from .....shared.colmap_io.reference_preview import ColmapReferencePreview
        from .....shared.colmap_io.validation import validate_reference_preview
    except ImportError:
        from shared.colmap_io.reference_preview import ColmapReferencePreview
        from shared.colmap_io.validation import validate_reference_preview
    selected = discover_sparse_model(layout.model_directory, input_format=layout.input_format)
    identities = tuple(identity_from_stat(path, path.stat()) for path in selected.selected_files)
    for identity in identities:
        if identity.size > limits.max_source_file_bytes:
            raise ColmapError("COLMAP 원본 파일이 크기 제한을 초과했습니다.")
    reader = create_incremental_sparse_model_reader(selected, preview_point_limit=point_limit)
    fingerprint = SourceFingerprintJob(selected)
    try:
        while not fingerprint.done:
            check_cancel(cancel_check)
            fingerprint.step(time_budget_seconds=0.01)
            yield 0, 1, "CAMERAS"
        while not reader.is_completed:
            check_cancel(cancel_check)
            if reader.stage == "POINTS3D":
                count = max(4096, limits.cancel_check_interval) if point_limit is not None else limits.cancel_check_interval
            else:
                count = 256 if point_limit is not None else 1
            progress = reader.step(max_records=count, time_budget_seconds=0.01)
            yield progress.current_count, progress.total_count or max(1, progress.current_count), progress.stage
        model = reader.model
        for expected in identities:
            if not expected.same_file(identity_from_stat(expected.path, expected.path.stat())):
                raise ColmapError("읽는 동안 COLMAP 원본 파일이 변경되었습니다.")
        cameras = {key: CameraRecord(item.camera_id, item.model_id, item.model_name, item.width, item.height, tuple(float(v) for v in item.params_float64)) for key, item in model.cameras.items()}
        images = {key: ImagePoseRecord(item.image_id, item.camera_id, tuple(float(v) for v in item.cam_from_world[:4]), tuple(float(v) for v in item.cam_from_world[4:]), item.name) for key, item in model.images.image_records.items()}
        validate_camera_image_records(cameras, images, limits)
        preview = model if isinstance(model, ColmapReferencePreview) else None
        if preview is not None:
            if preview.source_point_count > limits.max_points3d or preview.source_track_count > limits.max_track_elements:
                raise ColmapError("COLMAP 원본 점·관측 트랙 수가 안전 상한을 초과했습니다.")
            validate_reference_preview(model)
        else:
            validate_model_structure(model, allow_empty_points=True)
        return cameras, images, PointRecordMapping(model.points, preview=preview), identities, fingerprint.result
    except ColmapIOError as exc:
        raise ColmapError(str(exc)) from exc
    finally:
        fingerprint.close()
        reader.close()
