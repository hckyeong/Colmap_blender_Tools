"""정규화·bake 완료 COLMAP 모델의 결정적 구조 통합을 수행합니다."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np

from ..colmap_io import (
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
from .preflight import LoadedMergeSource, canonical_image_name, normalized_relative_image_name
from .types import MergeCancelledError, MergeExportError, MergeIssue, MergePolicy, MergeStage


class MergeEngineError(MergeExportError):
    """ID 재매핑·트랙 통합·출력 구조 조립 실패를 나타냅니다."""


ReprojectionErrorFunction = Callable[[CameraRecord, np.ndarray, np.ndarray, np.ndarray], float]
CancelRequestedFunction = Callable[[], bool]
ProgressFunction = Callable[[MergeStage, float, str, Mapping[str, int]], None]


@dataclass(frozen=True, slots=True)
class MergeIdMaps:
    """감사 NPZ와 역추적에 쓰는 source ordinal·old ID 기반 재매핑입니다."""

    rig_ids: Mapping[tuple[int, int], int]
    camera_ids: Mapping[tuple[int, int], int]
    frame_ids: Mapping[tuple[int, int], int]
    image_ids: Mapping[tuple[int, int], int]
    point3d_ids: Mapping[tuple[int, int], int]
    point3d_component_ids: Mapping[tuple[int, int], int]
    point3d_drop_reasons: Mapping[tuple[int, int], str]

    def __post_init__(self) -> None:
        """외부가 ID map을 바꿔 결과와 감사 파일이 달라지지 않게 고정합니다."""

        for field_name in (
            "rig_ids",
            "camera_ids",
            "frame_ids",
            "image_ids",
            "point3d_ids",
            "point3d_component_ids",
            "point3d_drop_reasons",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{field_name}는 mapping이어야 합니다.")
            object.__setattr__(self, field_name, MappingProxyType(dict(value)))

    @staticmethod
    def _arrays_for_map(mapping: Mapping[tuple[int, int], int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """한 ID mapping을 allow_pickle=False NPZ용 연속 숫자 배열로 만듭니다."""

        items = sorted(mapping.items())
        ordinals = np.asarray([key[0] for key, _ in items], dtype=np.uint32)
        old_ids = np.asarray([key[1] for key, _ in items], dtype=np.uint64)
        new_ids = np.asarray([value for _, value in items], dtype=np.uint64)
        return ordinals, old_ids, new_ids

    def to_npz_arrays(self) -> dict[str, np.ndarray]:
        """큰 JSON 대신 allow_pickle=False로 기록할 ID 감사 배열을 반환합니다."""

        arrays: dict[str, np.ndarray] = {}
        for label, mapping in (
            ("rig", self.rig_ids),
            ("camera", self.camera_ids),
            ("frame", self.frame_ids),
            ("image", self.image_ids),
        ):
            ordinals, old_ids, new_ids = self._arrays_for_map(mapping)
            arrays[f"{label}_source_model_ordinals"] = ordinals
            arrays[f"old_{label}_ids"] = old_ids
            arrays[f"new_{label}_ids"] = new_ids
        point_ordinals, point_old_ids, point_new_ids = self._arrays_for_map(self.point3d_ids)
        arrays["source_model_ordinals"] = point_ordinals
        arrays["old_point3d_ids"] = point_old_ids
        arrays["new_point3d_ids"] = point_new_ids
        point_items = sorted(self.point3d_ids)
        arrays["point3d_merge_component_ids"] = np.asarray(
            [self.point3d_component_ids[key] for key in point_items], dtype=np.uint64
        )
        arrays["point3d_drop_reasons"] = np.asarray(
            [self.point3d_drop_reasons.get(key, "") for key in point_items], dtype="<U32"
        )
        return arrays


@dataclass(frozen=True, slots=True)
class MergeEngineResult:
    """구조 통합 뒤 writer가 그대로 기록할 모델과 결정적 감사 정보를 보관합니다."""

    model: ColmapModelData
    id_maps: MergeIdMaps
    duplicate_image_group_count: int
    merged_point_component_count: int
    dropped_short_track_count: int
    warnings: tuple[MergeIssue, ...] = ()


class _DisjointSet:
    """packed observation key의 전이적 Point3D 연결 성분을 관리합니다."""

    def __init__(self, count: int) -> None:
        """연속 node ordinal에 맞는 parent·rank 배열을 초기화합니다."""

        self.parent = np.arange(count, dtype=np.uint64)
        self.rank = np.zeros(count, dtype=np.uint8)

    def find(self, node: int) -> int:
        """경로 압축으로 node의 대표 root를 반환합니다."""

        parent = self.parent
        current = int(node)
        while int(parent[current]) != current:
            parent[current] = parent[int(parent[current])]
            current = int(parent[current])
        return current

    def union(self, first: int, second: int) -> None:
        """두 Point3D node의 관측 연결 성분을 합칩니다."""

        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        if self.rank[first_root] < self.rank[second_root]:
            first_root, second_root = second_root, first_root
        self.parent[second_root] = first_root
        if self.rank[first_root] == self.rank[second_root]:
            self.rank[first_root] = np.uint8(int(self.rank[first_root]) + 1)


def _raise_if_cancelled(cancel_requested: CancelRequestedFunction | None) -> None:
    """대형 point·observation loop 사이에서 worker 취소 요청을 반영합니다."""

    if cancel_requested is not None and cancel_requested():
        raise MergeCancelledError("사용자가 구조 통합 내보내기 작업을 취소했습니다.")


def _emit_progress(
    callback: ProgressFunction | None,
    stage: MergeStage,
    fraction: float,
    message: str,
    counts: Mapping[str, int] | None = None,
) -> None:
    """엔진의 긴 하위 단계를 worker 진행률 callback으로 전달합니다."""

    if callback is not None:
        callback(stage, fraction, message, {} if counts is None else counts)


def _require_uint32(value: int, field_name: str) -> int:
    """COLMAP output ID·POINT2D_IDX가 uint32 범위에 드는지 확인합니다."""

    numeric = int(value)
    if numeric < 0 or numeric > int(np.iinfo(np.uint32).max):
        raise MergeEngineError(f"{field_name}가 uint32 범위를 벗어났습니다.")
    return numeric


def _ordered_sources(sources: Sequence[LoadedMergeSource]) -> tuple[LoadedMergeSource, ...]:
    """snapshot ordinal을 다시 검증해 엔진 입력 순서 의존성을 제거합니다."""

    ordered = tuple(sorted(sources, key=lambda source: source.ordinal))
    if not ordered:
        raise MergeEngineError("구조 통합할 A 그룹 모델이 없습니다.")
    expected = tuple(range(len(ordered)))
    actual = tuple(source.ordinal for source in ordered)
    if actual != expected:
        raise MergeEngineError("구조 통합 source ordinal이 0부터 연속적이지 않습니다.")
    anchors = tuple(source for source in ordered if source.snapshot.is_anchor)
    if len(anchors) != 1 or anchors[0].ordinal != 0:
        raise MergeEngineError("구조 통합 source 순서의 첫 모델은 유일한 Anchor여야 합니다.")
    return ordered


def _build_entity_id_map(
    sources: Sequence[LoadedMergeSource],
    attribute_name: str,
) -> dict[tuple[int, int], int]:
    """source ordinal·old ID 순서로 일반 전역 entity ID를 결정적으로 발급합니다."""

    result: dict[tuple[int, int], int] = {}
    next_id = 1
    for source in sources:
        entities = getattr(source.model, attribute_name)
        for old_id in sorted(entities):
            _require_uint32(next_id, f"새 {attribute_name} ID")
            result[(source.ordinal, int(old_id))] = next_id
            next_id += 1
    return result


def _build_image_id_map(
    sources: Sequence[LoadedMergeSource],
) -> tuple[dict[tuple[int, int], int], dict[int, tuple[LoadedMergeSource, int]], int]:
    """Anchor 우선·source order·old IMAGE_ID 순서로 basename 대표와 new IMAGE_ID를 정합니다."""

    canonical_to_new_id: dict[str, int] = {}
    image_id_map: dict[tuple[int, int], int] = {}
    representatives: dict[int, tuple[LoadedMergeSource, int]] = {}
    duplicate_group_names: set[str] = set()
    next_id = 1
    for source in sources:
        for old_image_id in sorted(source.model.images.image_records):
            image = source.model.images.image_records[old_image_id]
            try:
                canonical = canonical_image_name(image.name)
            except Exception as error:
                raise MergeEngineError(
                    f"IMAGE_ID {old_image_id}의 basename을 결정하지 못했습니다: {error}"
                ) from error
            new_id = canonical_to_new_id.get(canonical)
            if new_id is None:
                _require_uint32(next_id, "새 IMAGE_ID")
                new_id = next_id
                next_id += 1
                canonical_to_new_id[canonical] = new_id
                representatives[new_id] = (source, old_image_id)
            else:
                duplicate_group_names.add(canonical)
            image_id_map[(source.ordinal, int(old_image_id))] = new_id
    return image_id_map, representatives, len(duplicate_group_names)


def _build_point2d_offsets(
    sources: Sequence[LoadedMergeSource],
    image_ids: Mapping[tuple[int, int], int],
) -> dict[tuple[int, int], int]:
    """같은 output 이미지 안에서 source별 points2D 시작 offset을 결정합니다."""

    offsets: dict[tuple[int, int], int] = {}
    next_offset_by_image: dict[int, int] = defaultdict(int)
    maximum_length = int(np.iinfo(np.uint32).max) + 1
    for source in sources:
        for old_image_id in sorted(source.model.images.image_records):
            key = (source.ordinal, int(old_image_id))
            try:
                output_image_id = image_ids[key]
            except KeyError as error:
                raise MergeEngineError("points2D offset을 만들 IMAGE_ID 재매핑이 없습니다.") from error
            point_range = source.model.images.point_range(old_image_id)
            point_count = point_range.stop - point_range.start
            offset = next_offset_by_image[output_image_id]
            next_offset = offset + point_count
            if next_offset > maximum_length:
                raise MergeEngineError("통합 이미지의 POINT2D_IDX가 uint32 범위를 벗어납니다.")
            offsets[key] = offset
            next_offset_by_image[output_image_id] = next_offset
    return offsets


def _remap_camera_record(record: CameraRecord, new_id: int) -> CameraRecord:
    """camera 내부 파라미터는 그대로 두고 전역 ID만 바꾼 output record를 만듭니다."""

    return CameraRecord(
        camera_id=new_id,
        model_id=record.model_id,
        model_name=record.model_name,
        width=record.width,
        height=record.height,
        params_float64=record.params_float64,
    )


def _remap_rig_record(
    record: RigRecord,
    *,
    new_id: int,
    source_ordinal: int,
    camera_ids: Mapping[tuple[int, int], int],
) -> RigRecord:
    """bake된 rig의 camera sensor ID만 output camera ID로 재매핑합니다."""

    def remap_sensor(sensor: RigSensorRecord) -> RigSensorRecord:
        """한 rig sensor의 camera 참조를 결정적 output ID로 바꿉니다."""

        if sensor.sensor_type != "CAMERA":
            raise MergeEngineError("IMU 또는 지원하지 않는 rig sensor는 구조 통합에서 기록할 수 없습니다.")
        try:
            new_camera_id = camera_ids[(source_ordinal, int(sensor.sensor_id))]
        except KeyError as error:
            raise MergeEngineError("rig sensor가 재매핑되지 않은 CAMERA_ID를 참조합니다.") from error
        return RigSensorRecord(
            sensor_type="CAMERA",
            sensor_id=new_camera_id,
            sensor_from_rig=sensor.sensor_from_rig,
        )

    sensors = tuple(remap_sensor(sensor) for sensor in record.sensors)
    if not sensors:
        raise MergeEngineError("출력 rig에 sensor가 없습니다.")
    return RigRecord(rig_id=new_id, reference_sensor=sensors[0], sensors=sensors)


def _frame_camera_image_ids(
    source: LoadedMergeSource,
    frame: FrameRecord,
    image_ids: Mapping[tuple[int, int], int],
) -> tuple[int, ...]:
    """frame이 소유한 output CAMERA IMAGE_ID 집합을 정렬해 반환합니다."""

    result: list[int] = []
    for data in frame.data_ids:
        if data.sensor_type != "CAMERA":
            raise MergeEngineError("IMU 또는 지원하지 않는 frame data는 구조 통합에서 기록할 수 없습니다.")
        try:
            result.append(image_ids[(source.ordinal, int(data.data_id))])
        except KeyError as error:
            raise MergeEngineError("frame이 재매핑되지 않은 IMAGE_ID를 참조합니다.") from error
    return tuple(sorted(result))


def _build_frames(
    sources: Sequence[LoadedMergeSource],
    *,
    rig_ids: Mapping[tuple[int, int], int],
    camera_ids: Mapping[tuple[int, int], int],
    image_ids: Mapping[tuple[int, int], int],
) -> tuple[dict[int, FrameRecord], dict[tuple[int, int], int]]:
    """출력 image가 정확히 한 frame에 속하게 frame ID와 CAMERA data를 재구성합니다."""

    output_frames: dict[int, FrameRecord] = {}
    frame_ids: dict[tuple[int, int], int] = {}
    frame_for_image: dict[int, int] = {}
    next_id = 1
    for source in sources:
        for old_frame_id in sorted(source.model.frames):
            frame = source.model.frames[old_frame_id]
            mapped_images = _frame_camera_image_ids(source, frame, image_ids)
            if not mapped_images:
                raise MergeEngineError("frame에 CAMERA data가 없어 output image 소유권을 만들 수 없습니다.")
            existing_frames = {frame_for_image[image_id] for image_id in mapped_images if image_id in frame_for_image}
            if existing_frames:
                if len(mapped_images) != 1 or len(existing_frames) != 1:
                    raise MergeEngineError(
                        "동일 output image를 여러 frame에 안전하게 배치할 수 없는 rig topology입니다."
                    )
                frame_ids[(source.ordinal, int(old_frame_id))] = next(iter(existing_frames))
                continue
            _require_uint32(next_id, "새 FRAME_ID")
            try:
                new_rig_id = rig_ids[(source.ordinal, int(frame.rig_id))]
            except KeyError as error:
                raise MergeEngineError("frame이 재매핑되지 않은 RIG_ID를 참조합니다.") from error
            data_ids: list[FrameDataRecord] = []
            for data in frame.data_ids:
                if data.sensor_type != "CAMERA":
                    raise MergeEngineError("IMU 또는 지원하지 않는 frame data는 구조 통합에서 기록할 수 없습니다.")
                try:
                    new_camera_id = camera_ids[(source.ordinal, int(data.sensor_id))]
                    new_image_id = image_ids[(source.ordinal, int(data.data_id))]
                except KeyError as error:
                    raise MergeEngineError("frame camera 또는 image ID 재매핑이 누락되었습니다.") from error
                data_ids.append(
                    FrameDataRecord(
                        sensor_type="CAMERA",
                        sensor_id=new_camera_id,
                        data_id=new_image_id,
                    )
                )
            output_frames[next_id] = FrameRecord(
                frame_id=next_id,
                rig_id=new_rig_id,
                rig_from_world=frame.rig_from_world,
                data_ids=tuple(data_ids),
            )
            for image_id in mapped_images:
                frame_for_image[image_id] = next_id
            frame_ids[(source.ordinal, int(old_frame_id))] = next_id
            next_id += 1
    return output_frames, frame_ids


def _build_output_images(
    representatives: Mapping[int, tuple[LoadedMergeSource, int]],
    camera_ids: Mapping[tuple[int, int], int],
) -> dict[int, ImageRecord]:
    """basename 대표 source의 XY·camera·pose·상대 이미지 이름을 output image로 보존합니다."""

    output: dict[int, ImageRecord] = {}
    for new_image_id, (source, old_image_id) in sorted(representatives.items()):
        original = source.model.images.image_records[old_image_id]
        try:
            camera_id = camera_ids[(source.ordinal, int(original.camera_id))]
        except KeyError as error:
            raise MergeEngineError("대표 image가 재매핑되지 않은 CAMERA_ID를 참조합니다.") from error
        output[new_image_id] = ImageRecord(
            image_id=new_image_id,
            camera_id=camera_id,
            name=normalized_relative_image_name(original.name),
            cam_from_world=original.cam_from_world,
        )
    return output


@dataclass(frozen=True, slots=True)
class _PointNodes:
    """source Point3D를 결정적 연속 node ordinal로 표현하는 내부 배열 묶음입니다."""

    source_ordinals: np.ndarray
    point_indices: np.ndarray
    old_point_ids: np.ndarray
    positions: np.ndarray
    colors: np.ndarray
    source_by_node: tuple[LoadedMergeSource, ...]
    node_by_source_point_id: Mapping[tuple[int, int], int]


def _build_point_nodes(
    sources: Sequence[LoadedMergeSource],
    *,
    cancel_requested: CancelRequestedFunction | None,
) -> _PointNodes:
    """source·old POINT3D_ID 순서로 node ordinal을 발급합니다."""

    source_ordinals: list[int] = []
    point_indices: list[int] = []
    old_ids: list[int] = []
    positions: list[np.ndarray] = []
    colors: list[np.ndarray] = []
    source_by_node: list[LoadedMergeSource] = []
    node_by_key: dict[tuple[int, int], int] = {}
    for source in sources:
        order = np.argsort(source.model.points.point_ids, kind="stable")
        for point_index_value in order:
            if len(old_ids) % 8192 == 0:
                _raise_if_cancelled(cancel_requested)
            point_index = int(point_index_value)
            old_id = int(source.model.points.point_ids[point_index])
            node = len(old_ids)
            key = (source.ordinal, old_id)
            if key in node_by_key:
                raise MergeEngineError("source POINT3D_ID가 중복되었습니다.")
            node_by_key[key] = node
            source_ordinals.append(source.ordinal)
            point_indices.append(point_index)
            old_ids.append(old_id)
            positions.append(source.model.points.positions[point_index])
            colors.append(source.model.points.colors[point_index])
            source_by_node.append(source)
    if not old_ids:
        raise MergeEngineError("구조 통합할 POINT3D가 없습니다.")
    return _PointNodes(
        source_ordinals=np.asarray(source_ordinals, dtype=np.uint32),
        point_indices=np.asarray(point_indices, dtype=np.uint64),
        old_point_ids=np.asarray(old_ids, dtype=np.uint64),
        positions=np.ascontiguousarray(np.asarray(positions, dtype=np.float64)),
        colors=np.ascontiguousarray(np.asarray(colors, dtype=np.uint8)),
        source_by_node=tuple(source_by_node),
        node_by_source_point_id=MappingProxyType(node_by_key),
    )


def _build_packed_observations(
    nodes: _PointNodes,
    image_ids: Mapping[tuple[int, int], int],
    *,
    cancel_requested: CancelRequestedFunction | None,
) -> tuple[np.ndarray, np.ndarray]:
    """`(new IMAGE_ID, POINT2D_IDX)`를 uint64 key로 pack해 DSU 입력을 만듭니다."""

    total = sum(
        int(source.model.points.track_image_ids.shape[0])
        for source in {source.ordinal: source for source in nodes.source_by_node}.values()
    )
    keys = np.empty(total, dtype=np.uint64)
    observation_nodes = np.empty(total, dtype=np.uint64)
    cursor = 0
    for node, source in enumerate(nodes.source_by_node):
        if node % 8192 == 0:
            _raise_if_cancelled(cancel_requested)
        point_index = int(nodes.point_indices[node])
        track = source.model.points.track_range(point_index)
        for track_index in range(track.start, track.stop):
            old_image_id = int(source.model.points.track_image_ids[track_index])
            point2d_index = _require_uint32(
                int(source.model.points.track_point2d_indices[track_index]),
                "POINT2D_IDX",
            )
            try:
                new_image_id = image_ids[(source.ordinal, old_image_id)]
            except KeyError as error:
                raise MergeEngineError("POINT3D track이 재매핑되지 않은 IMAGE_ID를 참조합니다.") from error
            _require_uint32(new_image_id, "새 IMAGE_ID")
            keys[cursor] = np.uint64((int(new_image_id) << 32) | point2d_index)
            observation_nodes[cursor] = np.uint64(node)
            cursor += 1
    if cursor != total:
        raise MergeEngineError("packed observation 배열 길이가 source track 수와 다릅니다.")
    return keys, observation_nodes


def _union_shared_observations(
    keys: np.ndarray,
    nodes: np.ndarray,
    point_count: int,
    *,
    cancel_requested: CancelRequestedFunction | None,
) -> _DisjointSet:
    """정렬된 동일 packed observation key의 node를 union해 전이적 트랙 성분을 만듭니다."""

    dsu = _DisjointSet(point_count)
    if keys.size == 0:
        return dsu
    order = np.argsort(keys, kind="stable")
    ordered_keys = keys[order]
    ordered_nodes = nodes[order]
    start = 0
    while start < ordered_keys.size:
        if start % 8192 == 0:
            _raise_if_cancelled(cancel_requested)
        stop = start + 1
        while stop < ordered_keys.size and ordered_keys[stop] == ordered_keys[start]:
            stop += 1
        first_node = int(ordered_nodes[start])
        for index in range(start + 1, stop):
            dsu.union(first_node, int(ordered_nodes[index]))
        start = stop
    return dsu


def _component_nodes(
    dsu: _DisjointSet,
    count: int,
    *,
    cancel_requested: CancelRequestedFunction | None,
) -> dict[int, list[int]]:
    """DSU root별 node 목록을 결정적 node ordinal 순서로 반환합니다."""

    components: dict[int, list[int]] = defaultdict(list)
    for node in range(count):
        if node % 8192 == 0:
            _raise_if_cancelled(cancel_requested)
        components[dsu.find(node)].append(node)
    return dict(components)


def _component_observation_keys(
    dsu: _DisjointSet,
    packed_keys: np.ndarray,
    observation_nodes: np.ndarray,
    *,
    cancel_requested: CancelRequestedFunction | None,
) -> dict[int, np.ndarray]:
    """각 DSU 성분의 중복 제거된 output observation key 배열을 반환합니다."""

    by_root: dict[int, list[int]] = defaultdict(list)
    for index, (key, node) in enumerate(zip(packed_keys, observation_nodes, strict=True)):
        if index % 8192 == 0:
            _raise_if_cancelled(cancel_requested)
        by_root[dsu.find(int(node))].append(int(key))
    return {
        root: np.unique(np.asarray(values, dtype=np.uint64))
        for root, values in by_root.items()
    }


def _check_same_source_component_collision(
    components: Mapping[int, Sequence[int]],
    nodes: _PointNodes,
) -> None:
    """한 source의 서로 다른 Point3D가 한 merged component에 들어가면 즉시 중단합니다."""

    for component_nodes in components.values():
        source_ordinals = [int(nodes.source_ordinals[node]) for node in component_nodes]
        if len(set(source_ordinals)) != len(source_ordinals):
            raise MergeEngineError(
                "한 source의 서로 다른 POINT3D가 동일 output 관측으로 전이적으로 연결되었습니다. "
                "원본 관측 구조를 보존할 수 없어 자동 병합하지 않습니다."
            )


def _select_representative_node(
    component_nodes: Sequence[int],
    nodes: _PointNodes,
    node_observation_counts: np.ndarray,
) -> int:
    """Anchor 우선, 관측 수, source 순서, old ID 순서로 대표 Point3D를 하나 고릅니다."""

    anchor_nodes = [node for node in component_nodes if int(nodes.source_ordinals[node]) == 0]
    if anchor_nodes:
        return min(anchor_nodes, key=lambda node: int(nodes.old_point_ids[node]))
    return min(
        component_nodes,
        key=lambda node: (
            -int(node_observation_counts[node]),
            int(nodes.source_ordinals[node]),
            int(nodes.old_point_ids[node]),
        ),
    )


def _decode_observation_key(key: int) -> tuple[int, int]:
    """uint64 packed observation key를 `(IMAGE_ID, POINT2D_IDX)`로 분리합니다."""

    return int(key >> 32), int(key & int(np.iinfo(np.uint32).max))


def _output_image_xy(output_images: Mapping[int, ImageRecord], sources_by_image: Mapping[int, tuple[LoadedMergeSource, int]], image_id: int, point2d_index: int) -> tuple[CameraRecord, np.ndarray, np.ndarray]:
    """대표 output image의 camera·pose·POINT2D XY를 reprojection 계산용으로 반환합니다."""

    try:
        source, old_image_id = sources_by_image[image_id]
        image = output_images[image_id]
    except KeyError as error:
        raise MergeEngineError("output observation이 존재하지 않는 대표 image를 참조합니다.") from error
    point_range = source.model.images.point_range(old_image_id)
    length = point_range.stop - point_range.start
    if point2d_index >= length:
        raise MergeEngineError("output observation의 POINT2D_IDX가 대표 image 범위를 벗어났습니다.")
    camera = source.model.cameras[source.model.images.image_records[old_image_id].camera_id]
    return camera, image.cam_from_world, source.model.images.point2d_xy[point_range.start + point2d_index]


def _ordered_track_arrays(
    points: PointTable,
    order: np.ndarray,
    keep_ordered: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """POINT3D_ID 순서와 short-track 제외를 반영한 track 배열과 길이를 반환합니다."""

    lengths = np.diff(points.track_offsets).astype(np.uint64, copy=False)
    ordered_lengths = lengths[order]
    is_identity_order = np.array_equal(order, np.arange(points.count, dtype=order.dtype))
    if is_identity_order:
        track_image_ids = points.track_image_ids
        track_point2d_indices = points.track_point2d_indices
    else:
        point_by_observation = np.repeat(
            np.arange(points.count, dtype=np.uint64),
            lengths.astype(np.intp, copy=False),
        )
        rank_by_point = np.empty(points.count, dtype=np.uint64)
        rank_by_point[order] = np.arange(points.count, dtype=np.uint64)
        observation_order = np.argsort(rank_by_point[point_by_observation], kind="stable")
        track_image_ids = points.track_image_ids[observation_order]
        track_point2d_indices = points.track_point2d_indices[observation_order]
    if not bool(np.all(keep_ordered)):
        keep_observations = np.repeat(
            keep_ordered,
            ordered_lengths.astype(np.intp, copy=False),
        )
        track_image_ids = track_image_ids[keep_observations]
        track_point2d_indices = track_point2d_indices[keep_observations]
    return track_image_ids, track_point2d_indices, ordered_lengths[keep_ordered]


def _remap_track_image_ids(
    source: LoadedMergeSource,
    old_track_image_ids: np.ndarray,
    image_ids: Mapping[tuple[int, int], int],
) -> np.ndarray:
    """한 source의 track IMAGE_ID를 정렬 검색으로 output IMAGE_ID에 일괄 매핑합니다."""

    old_ids = np.asarray(sorted(source.model.images.image_records), dtype=np.uint64)
    new_ids = np.asarray(
        [image_ids[(source.ordinal, int(old_id))] for old_id in old_ids],
        dtype=np.uint64,
    )
    indices = np.searchsorted(old_ids, old_track_image_ids)
    if (
        indices.size
        and (
            bool(np.any(indices >= old_ids.size))
            or not np.array_equal(old_ids[indices], old_track_image_ids)
        )
    ):
        raise MergeEngineError("POINT3D track IMAGE_ID 일괄 재매핑이 source 구조와 다릅니다.")
    return np.ascontiguousarray(new_ids[indices], dtype=np.uint64)


def _remap_track_point2d_indices(
    source: LoadedMergeSource,
    old_track_image_ids: np.ndarray,
    old_track_point2d_indices: np.ndarray,
    point2d_offsets: Mapping[tuple[int, int], int],
) -> np.ndarray:
    """한 source의 POINT2D_IDX에 output 이미지별 누적 offset을 적용합니다."""

    old_ids = np.asarray(sorted(source.model.images.image_records), dtype=np.uint64)
    offsets = np.asarray(
        [point2d_offsets[(source.ordinal, int(old_id))] for old_id in old_ids],
        dtype=np.uint64,
    )
    indices = np.searchsorted(old_ids, old_track_image_ids)
    if (
        indices.size
        and (
            bool(np.any(indices >= old_ids.size))
            or not np.array_equal(old_ids[indices], old_track_image_ids)
        )
    ):
        raise MergeEngineError("POINT3D track POINT2D_IDX의 IMAGE_ID가 source 구조와 다릅니다.")
    remapped = offsets[indices] + old_track_point2d_indices.astype(np.uint64, copy=False)
    if remapped.size and bool(np.any(remapped > np.iinfo(np.uint32).max)):
        raise MergeEngineError("재매핑한 POINT2D_IDX가 uint32 범위를 벗어납니다.")
    return np.ascontiguousarray(remapped, dtype=np.uint32)


def _build_unshared_output_points(
    sources: Sequence[LoadedMergeSource],
    image_ids: Mapping[tuple[int, int], int],
    point2d_offsets: Mapping[tuple[int, int], int],
    *,
    cancel_requested: CancelRequestedFunction | None,
    progress: ProgressFunction | None,
) -> tuple[
    PointTable,
    dict[tuple[int, int], int],
    dict[tuple[int, int], int],
    dict[tuple[int, int], str],
    int,
]:
    """source 점을 전역 ID로 잇고 이미지별 offset을 track POINT2D_IDX에 적용합니다."""

    output_positions: list[np.ndarray] = []
    output_colors: list[np.ndarray] = []
    output_errors: list[np.ndarray] = []
    output_track_image_ids: list[np.ndarray] = []
    output_track_point2d_indices: list[np.ndarray] = []
    track_offsets = [np.asarray((0,), dtype=np.uint64)]
    source_point_ids: dict[tuple[int, int], int] = {}
    component_ids: dict[tuple[int, int], int] = {}
    drop_reasons: dict[tuple[int, int], str] = {}
    next_point_id = 1
    next_component_id = 1
    observation_total = 0
    dropped_short = 0
    total_sources = len(sources)
    for source_index, source in enumerate(sources):
        _raise_if_cancelled(cancel_requested)
        points = source.model.points
        order = np.argsort(points.point_ids, kind="stable")
        ordered_old_ids = points.point_ids[order]
        ordered_lengths = np.diff(points.track_offsets)[order]
        keep_ordered = ordered_lengths >= 2
        kept_count = int(np.count_nonzero(keep_ordered))
        kept_new_ids = np.arange(
            next_point_id,
            next_point_id + kept_count,
            dtype=np.uint64,
        )
        kept_cursor = 0
        for ordered_index, old_id_value in enumerate(ordered_old_ids):
            if ordered_index % 8192 == 0:
                _raise_if_cancelled(cancel_requested)
            key = (source.ordinal, int(old_id_value))
            component_ids[key] = next_component_id
            next_component_id += 1
            if keep_ordered[ordered_index]:
                source_point_ids[key] = int(kept_new_ids[kept_cursor])
                kept_cursor += 1
            else:
                source_point_ids[key] = INVALID_POINT3D_ID
                drop_reasons[key] = "SHORT_TRACK"
                dropped_short += 1
        output_positions.append(np.ascontiguousarray(points.positions[order][keep_ordered]))
        output_colors.append(np.ascontiguousarray(points.colors[order][keep_ordered]))
        output_errors.append(np.ascontiguousarray(points.errors[order][keep_ordered]))
        old_track_images, track_indices, kept_lengths = _ordered_track_arrays(
            points,
            order,
            keep_ordered,
        )
        remapped_track_images = _remap_track_image_ids(source, old_track_images, image_ids)
        output_track_image_ids.append(remapped_track_images)
        output_track_point2d_indices.append(
            _remap_track_point2d_indices(
                source,
                old_track_images,
                track_indices,
                point2d_offsets,
            )
        )
        cumulative = np.cumsum(kept_lengths, dtype=np.uint64) + np.uint64(observation_total)
        track_offsets.append(cumulative)
        if cumulative.size:
            observation_total = int(cumulative[-1])
        next_point_id += kept_count
        _emit_progress(
            progress,
            MergeStage.MERGE_POINT_TRACKS,
            0.72 + 0.06 * ((source_index + 1) / total_sources),
            "Point3D 전역 ID와 이미지별 POINT2D offset을 재매핑하고 있습니다.",
            {
                "source_models": source_index + 1,
                "total_source_models": total_sources,
                "points3d": next_point_id - 1,
                "observations": observation_total,
            },
        )
    if next_point_id == 1:
        raise MergeEngineError("2개 이상 고유 관측을 가진 output Point3D가 없어 구조 통합을 기록할 수 없습니다.")
    positions = np.concatenate(output_positions, axis=0)
    colors = np.concatenate(output_colors, axis=0)
    errors = np.concatenate(output_errors, axis=0)
    track_image_ids = np.concatenate(output_track_image_ids)
    track_point2d_indices = np.concatenate(output_track_point2d_indices)
    offsets = np.concatenate(track_offsets)
    if int(offsets[-1]) != int(track_image_ids.size):
        raise MergeEngineError("직접 병합 track offset과 관측 배열 길이가 다릅니다.")
    point_count = next_point_id - 1
    return (
        PointTable(
            point_ids=np.arange(1, next_point_id, dtype=np.uint64),
            positions=positions,
            colors=colors,
            errors=errors,
            track_offsets=offsets,
            track_image_ids=track_image_ids,
            track_point2d_indices=track_point2d_indices,
            display_indices=np.arange(point_count, dtype=np.uint64),
        ),
        source_point_ids,
        component_ids,
        drop_reasons,
        dropped_short,
    )


def _build_output_points(
    *,
    components: Mapping[int, Sequence[int]],
    component_keys: Mapping[int, np.ndarray],
    nodes: _PointNodes,
    output_images: Mapping[int, ImageRecord],
    representatives: Mapping[int, tuple[LoadedMergeSource, int]],
    policy: MergePolicy,
    reprojection_error: ReprojectionErrorFunction,
    cancel_requested: CancelRequestedFunction | None,
) -> tuple[PointTable, dict[tuple[int, int], int], dict[tuple[int, int], int], dict[tuple[int, int], str], dict[tuple[int, int], int], int]:
    """대표점·short-track drop·재투영 검증을 적용한 PointTable과 source point ID map을 만듭니다."""

    if reprojection_error is None:
        raise MergeEngineError(
            "병합 Point3D 전체 트랙의 재투영 오차 계산기가 없어 구조 통합을 진행할 수 없습니다."
        )
    node_counts = np.zeros(nodes.old_point_ids.shape[0], dtype=np.uint64)
    for root_nodes in components.values():
        for node in root_nodes:
            node_counts[node] = np.uint64(
                int(nodes.source_by_node[node].model.points.track_range(int(nodes.point_indices[node])).stop)
                - int(nodes.source_by_node[node].model.points.track_range(int(nodes.point_indices[node])).start)
            )
    component_entries: list[tuple[tuple[int, int], int, int]] = []
    for component_ordinal, (root, component_nodes) in enumerate(sorted(components.items()), start=1):
        representative = _select_representative_node(component_nodes, nodes, node_counts)
        component_entries.append(
            (
                (int(nodes.source_ordinals[representative]), int(nodes.old_point_ids[representative])),
                root,
                component_ordinal,
            )
        )
    component_entries.sort(key=lambda item: item[0])
    source_point_ids: dict[tuple[int, int], int] = {}
    component_ids: dict[tuple[int, int], int] = {}
    drop_reasons: dict[tuple[int, int], str] = {}
    output_ids: list[int] = []
    positions: list[np.ndarray] = []
    colors: list[np.ndarray] = []
    errors: list[float] = []
    tracks_image_ids: list[int] = []
    tracks_point2d_indices: list[int] = []
    track_offsets = [0]
    next_point_id = 1
    dropped_short = 0
    representative_by_image = dict(representatives)
    for component_index, (_representative_key, root, component_ordinal) in enumerate(component_entries):
        if component_index % 1024 == 0:
            _raise_if_cancelled(cancel_requested)
        component_nodes = components[root]
        for node in component_nodes:
            key = (int(nodes.source_ordinals[node]), int(nodes.old_point_ids[node]))
            component_ids[key] = component_ordinal
        observation_keys = component_keys.get(root, np.empty(0, dtype=np.uint64))
        if observation_keys.size < 2:
            dropped_short += 1
            for node in component_nodes:
                key = (int(nodes.source_ordinals[node]), int(nodes.old_point_ids[node]))
                source_point_ids[key] = INVALID_POINT3D_ID
                drop_reasons[key] = "SHORT_TRACK"
            continue
        representative = _select_representative_node(component_nodes, nodes, node_counts)
        position = np.asarray(nodes.positions[representative], dtype=np.float64)
        residuals: list[float] = []
        decoded: list[tuple[int, int]] = []
        for observation_index, packed_key in enumerate(observation_keys):
            if observation_index % 8192 == 0:
                _raise_if_cancelled(cancel_requested)
            image_id, point2d_index = _decode_observation_key(int(packed_key))
            camera, cam_from_world, observed_xy = _output_image_xy(
                output_images,
                representative_by_image,
                image_id,
                point2d_index,
            )
            try:
                residual = float(reprojection_error(camera, cam_from_world, position, observed_xy))
            except Exception as error:
                raise MergeEngineError("merged Point3D 재투영 오차 계산에 실패했습니다.") from error
            if not np.isfinite(residual) or residual < 0.0:
                raise MergeEngineError("merged Point3D 재투영 오차가 유한한 0 이상 수치가 아닙니다.")
            residuals.append(residual)
            decoded.append((image_id, point2d_index))
        maximum_residual = max(residuals)
        mean_residual = float(np.mean(np.asarray(residuals, dtype=np.float64)))
        limit = policy.merged_point_max_reprojection_error_pixels
        if limit is not None and (mean_residual > limit or maximum_residual > limit):
            raise MergeEngineError(
                "merged Point3D 대표 좌표의 전체 트랙 재투영 오차가 정책 한도를 넘었습니다. "
                "다른 대표점을 자동 선택하거나 관측을 삭제하지 않습니다."
            )
        _require_uint32(next_point_id, "새 POINT3D_ID")
        output_ids.append(next_point_id)
        positions.append(position)
        colors.append(nodes.colors[representative])
        errors.append(mean_residual)
        for image_id, point2d_index in decoded:
            tracks_image_ids.append(image_id)
            tracks_point2d_indices.append(point2d_index)
        track_offsets.append(len(tracks_image_ids))
        for node in component_nodes:
            key = (int(nodes.source_ordinals[node]), int(nodes.old_point_ids[node]))
            source_point_ids[key] = next_point_id
        next_point_id += 1
    if not output_ids:
        raise MergeEngineError("2개 이상 고유 관측을 가진 output Point3D가 없어 구조 통합을 기록할 수 없습니다.")
    point_table = PointTable(
        point_ids=np.asarray(output_ids, dtype=np.uint64),
        positions=np.ascontiguousarray(np.asarray(positions, dtype=np.float64)),
        colors=np.ascontiguousarray(np.asarray(colors, dtype=np.uint8)),
        errors=np.asarray(errors, dtype=np.float64),
        track_offsets=np.asarray(track_offsets, dtype=np.uint64),
        track_image_ids=np.asarray(tracks_image_ids, dtype=np.uint64),
        track_point2d_indices=np.asarray(tracks_point2d_indices, dtype=np.uint32),
        display_indices=np.arange(len(output_ids), dtype=np.uint64),
    )
    return (
        point_table,
        source_point_ids,
        component_ids,
        drop_reasons,
        {root: ordinal for _key, root, ordinal in component_entries},
        dropped_short,
    )


def _build_image_table_with_point2d_offsets(
    output_images: Mapping[int, ImageRecord],
    sources: Sequence[LoadedMergeSource],
    image_ids: Mapping[tuple[int, int], int],
    point2d_offsets: Mapping[tuple[int, int], int],
    points: PointTable,
    *,
    cancel_requested: CancelRequestedFunction | None,
) -> ImageTable:
    """source별 points2D를 offset 순서로 잇고 역방향 POINT3D_ID 링크를 만듭니다."""

    xy_by_image: dict[int, list[np.ndarray]] = {
        image_id: [] for image_id in output_images
    }
    point_count_by_image: dict[int, int] = defaultdict(int)
    for source in sources:
        for old_image_id in sorted(source.model.images.image_records):
            key = (source.ordinal, int(old_image_id))
            try:
                output_image_id = image_ids[key]
                expected_offset = point2d_offsets[key]
            except KeyError as error:
                raise MergeEngineError("points2D 배열을 조립할 이미지 재매핑이 없습니다.") from error
            if point_count_by_image[output_image_id] != expected_offset:
                raise MergeEngineError("points2D 누적 길이와 결정된 offset이 다릅니다.")
            point_range = source.model.images.point_range(old_image_id)
            xy = np.ascontiguousarray(
                source.model.images.point2d_xy[point_range],
                dtype=np.float64,
            )
            xy_by_image[output_image_id].append(xy)
            point_count_by_image[output_image_id] += int(xy.shape[0])

    offsets = [0]
    xy_parts: list[np.ndarray] = []
    for image_id in sorted(output_images):
        parts = xy_by_image[image_id]
        xy = (
            np.ascontiguousarray(np.concatenate(parts, axis=0), dtype=np.float64)
            if parts
            else np.empty((0, 2), dtype=np.float64)
        )
        offsets.append(offsets[-1] + xy.shape[0])
        xy_parts.append(xy)
    total_points2d = offsets[-1]
    combined_links = np.full(total_points2d, INVALID_POINT3D_ID, dtype=np.uint64)
    if points.track_image_ids.size:
        _raise_if_cancelled(cancel_requested)
        sorted_image_ids = np.asarray(sorted(output_images), dtype=np.uint64)
        image_indices = np.searchsorted(sorted_image_ids, points.track_image_ids)
        if (
            bool(np.any(image_indices >= sorted_image_ids.size))
            or not np.array_equal(sorted_image_ids[image_indices], points.track_image_ids)
        ):
            raise MergeEngineError("output Point3D track이 존재하지 않는 IMAGE_ID를 참조합니다.")
        offset_array = np.asarray(offsets, dtype=np.uint64)
        target_indices = (
            offset_array[image_indices]
            + points.track_point2d_indices.astype(np.uint64, copy=False)
        )
        if bool(np.any(target_indices >= offset_array[image_indices + 1])):
            raise MergeEngineError("output Point3D track의 POINT2D_IDX가 통합 image 범위를 벗어났습니다.")
        if np.unique(target_indices).size != target_indices.size:
            raise MergeEngineError("하나의 output observation key가 둘 이상의 Point3D를 가리킵니다.")
        track_lengths = np.diff(points.track_offsets).astype(np.intp, copy=False)
        point_ids_by_observation = np.repeat(points.point_ids, track_lengths)
        combined_links[target_indices] = point_ids_by_observation
    xy = (
        np.ascontiguousarray(np.concatenate(xy_parts, axis=0), dtype=np.float64)
        if xy_parts
        else np.empty((0, 2), dtype=np.float64)
    )
    return ImageTable(
        image_records=output_images,
        point2d_offsets=np.asarray(offsets, dtype=np.uint64),
        point2d_xy=xy,
        point2d_point3d_ids=combined_links,
    )


def merge_baked_models(
    sources: Sequence[LoadedMergeSource],
    *,
    output_source_path: Path,
    output_model_id: str,
    cancel_requested: CancelRequestedFunction | None = None,
    progress: ProgressFunction | None = None,
) -> MergeEngineResult:
    """bake·modern 정규화된 모델을 basename과 source별 POINT2D offset으로 통합합니다."""

    ordered_sources = _ordered_sources(sources)
    for source in ordered_sources:
        if not source.model.rigs or not source.model.frames:
            raise MergeEngineError(
                "merge engine 입력은 legacy가 아닌 rigs·frames 정규화 완료 모델이어야 합니다."
            )
    _emit_progress(
        progress,
        MergeStage.REMAP_RIGS_CAMERAS_FRAMES,
        0.68,
        "rig·camera·frame 전역 ID를 재매핑하고 있습니다.",
    )
    rig_ids = _build_entity_id_map(ordered_sources, "rigs")
    camera_ids = _build_entity_id_map(ordered_sources, "cameras")
    image_ids, representatives, duplicate_image_group_count = _build_image_id_map(ordered_sources)
    point2d_offsets = _build_point2d_offsets(ordered_sources, image_ids)
    _emit_progress(
        progress,
        MergeStage.MERGE_IMAGES_BY_BASENAME,
        0.70,
        "basename 기준 output 이미지 대표와 ID를 확정했습니다.",
        {
            "images": len(representatives),
            "duplicate_image_groups": duplicate_image_group_count,
        },
    )
    output_rigs: dict[int, RigRecord] = {}
    output_cameras: dict[int, CameraRecord] = {}
    for source in ordered_sources:
        for old_rig_id in sorted(source.model.rigs):
            new_rig_id = rig_ids[(source.ordinal, int(old_rig_id))]
            output_rigs[new_rig_id] = _remap_rig_record(
                source.model.rigs[old_rig_id],
                new_id=new_rig_id,
                source_ordinal=source.ordinal,
                camera_ids=camera_ids,
            )
        for old_camera_id in sorted(source.model.cameras):
            new_camera_id = camera_ids[(source.ordinal, int(old_camera_id))]
            output_cameras[new_camera_id] = _remap_camera_record(
                source.model.cameras[old_camera_id],
                new_camera_id,
            )
    output_frames, frame_ids = _build_frames(
        ordered_sources,
        rig_ids=rig_ids,
        camera_ids=camera_ids,
        image_ids=image_ids,
    )
    output_images = _build_output_images(representatives, camera_ids)
    (
        output_points,
        point3d_ids,
        component_ids,
        drop_reasons,
        dropped_short_track_count,
    ) = _build_unshared_output_points(
        ordered_sources,
        image_ids,
        point2d_offsets,
        cancel_requested=cancel_requested,
        progress=progress,
    )
    _emit_progress(
        progress,
        MergeStage.BUILD_BIDIRECTIONAL_LINKS,
        0.80,
        "Image points2D와 Point3D track의 역방향 링크를 만들고 있습니다.",
        {
            "points3d": output_points.count,
            "observations": int(output_points.track_image_ids.size),
        },
    )
    output_image_table = _build_image_table_with_point2d_offsets(
        output_images,
        ordered_sources,
        image_ids,
        point2d_offsets,
        output_points,
        cancel_requested=cancel_requested,
    )
    bounds_min, bounds_max = calculate_bounds(output_points.positions)
    output_model = ColmapModelData(
        model_id=str(output_model_id),
        display_name="Merged COLMAP",
        source_path=Path(output_source_path),
        source_format="binary",
        cameras=output_cameras,
        images=output_image_table,
        rigs=output_rigs,
        frames=output_frames,
        points=output_points,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        geometry_revision=1,
    )
    validate_model(output_model)
    return MergeEngineResult(
        model=output_model,
        id_maps=MergeIdMaps(
            rig_ids=rig_ids,
            camera_ids=camera_ids,
            frame_ids=frame_ids,
            image_ids=image_ids,
            point3d_ids=point3d_ids,
            point3d_component_ids=component_ids,
            point3d_drop_reasons=drop_reasons,
        ),
        duplicate_image_group_count=duplicate_image_group_count,
        merged_point_component_count=0,
        dropped_short_track_count=dropped_short_track_count,
    )


__all__ = (
    "MergeEngineError",
    "MergeEngineResult",
    "MergeIdMaps",
    "ReprojectionErrorFunction",
    "merge_baked_models",
)
