"""Export staging Reconstruction 무결성 검사를 재진입 가능하게 실행합니다."""

from __future__ import annotations

import math
import time
import gc
from enum import Enum, auto

from .camera_models import model_by_id, pinhole_parameters
from .model import ImageRecord, Point3DRecord, Reconstruction


class _ValidationPhase(Enum):
    CAMERAS = auto()
    IMAGES = auto()
    POINTS3D = auto()
    EXPECTED_TRACKS = auto()
    MISMATCHED_TRACKS = auto()
    RIGS = auto()
    FRAMES = auto()
    MISSING_FRAME_IMAGES = auto()
    COMPLETE = auto()


class ReconstructionValidationJob:
    """기존 무결성 검사 순서와 메시지를 유지하며 순회를 나눕니다."""

    _PHASE_RANGES = {
        _ValidationPhase.CAMERAS: (0.0, 0.05),
        _ValidationPhase.IMAGES: (0.05, 0.40),
        _ValidationPhase.POINTS3D: (0.40, 0.70),
        _ValidationPhase.EXPECTED_TRACKS: (0.70, 0.78),
        _ValidationPhase.MISMATCHED_TRACKS: (0.78, 0.82),
        _ValidationPhase.RIGS: (0.82, 0.90),
        _ValidationPhase.FRAMES: (0.90, 0.98),
        _ValidationPhase.MISSING_FRAME_IMAGES: (0.98, 1.0),
        _ValidationPhase.COMPLETE: (1.0, 1.0),
    }

    def __init__(self, model: Reconstruction) -> None:
        self._model = model
        self._phase = _ValidationPhase.CAMERAS
        self._errors: list[str] = []
        self._warnings: list[str] = []
        self._expected_track_pairs: dict[tuple[int, int], int] = {}
        self._actual_track_pairs: dict[tuple[int, int], int] = {}
        self._mismatched_track_pairs: set[tuple[int, int]] = set()
        self._image_to_frame: dict[int, int] = {}
        self._iterator = iter(model.cameras.items())
        self._current_image: ImageRecord | None = None
        self._current_image_point_index = 0
        self._current_point: Point3DRecord | None = None
        self._current_track_index = 0
        self._mismatch_iterator = None
        self._result: tuple[str, ...] | None = None
        self._phase_completed = 0
        self._phase_total = len(model.cameras)

    @property
    def done(self) -> bool:
        return self._phase is _ValidationPhase.COMPLETE

    @property
    def result(self) -> tuple[str, ...]:
        if self._result is None:
            raise RuntimeError("완료되지 않은 무결성 검사 Job의 결과를 읽을 수 없습니다.")
        return self._result

    @property
    def fraction(self) -> float:
        start, end = self._PHASE_RANGES[self._phase]
        if self._phase_total:
            local = min(1.0, self._phase_completed / self._phase_total)
        else:
            local = 0.0
        return start + ((end - start) * local)

    @property
    def completed_units(self) -> int:
        """현재 무결성 하위 단계에서 완료한 실제 레코드 수입니다."""

        return self._phase_completed

    @property
    def total_units(self) -> int:
        """현재 무결성 하위 단계의 실제 전체 레코드 수입니다."""

        return self._phase_total

    def advance(self, work_budget_ms: float | None) -> None:
        if self.done:
            raise RuntimeError("완료된 무결성 검사 Job을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            while not self.done:
                batch_phase = self._phase
                if self._phase is _ValidationPhase.IMAGES:
                    self._advance_image_batch(2_048)
                elif self._phase is _ValidationPhase.POINTS3D:
                    self._advance_point3d_batch(2_048)
                elif self._phase is _ValidationPhase.EXPECTED_TRACKS:
                    self._advance_expected_track_batch(2_048)
                else:
                    for _ in range(2_048):
                        if self.done:
                            break
                        self._advance_one()
                        if self._phase is not batch_phase:
                            break
                if deadline is not None and time.perf_counter() >= deadline:
                    return
        finally:
            if gc_was_enabled:
                gc.enable()
                gc.collect(0)

    def _advance_one(self) -> None:
        if self._phase is _ValidationPhase.CAMERAS:
            self._advance_camera()
        elif self._phase is _ValidationPhase.IMAGES:
            self._advance_image()
        elif self._phase is _ValidationPhase.POINTS3D:
            self._advance_point3d()
        elif self._phase is _ValidationPhase.EXPECTED_TRACKS:
            self._advance_expected_track()
        elif self._phase is _ValidationPhase.MISMATCHED_TRACKS:
            self._advance_mismatched_track()
        elif self._phase is _ValidationPhase.RIGS:
            self._advance_rig()
        elif self._phase is _ValidationPhase.FRAMES:
            self._advance_frame()
        elif self._phase is _ValidationPhase.MISSING_FRAME_IMAGES:
            self._advance_missing_frame_image()
        else:
            raise RuntimeError(f"알 수 없는 무결성 검사 단계입니다: {self._phase}")

    def _next_or_phase(self, next_phase: _ValidationPhase, source) -> tuple | None:
        try:
            value = next(self._iterator)
        except StopIteration:
            self._set_phase(next_phase, source)
            return None
        self._phase_completed += 1
        return value

    def _set_phase(self, phase: _ValidationPhase, source) -> None:
        self._phase = phase
        self._iterator = iter(source)
        self._phase_completed = 0
        try:
            self._phase_total = len(source)
        except TypeError:
            self._phase_total = 0

    def _advance_camera(self) -> None:
        item = self._next_or_phase(
            _ValidationPhase.IMAGES,
            self._model.images.items(),
        )
        if item is None:
            return
        camera_key, camera = item
        if camera_key != camera.camera_id:
            self._errors.append(
                f"Camera dict key {camera_key}와 레코드 CAMERA_ID "
                f"{camera.camera_id}가 다릅니다."
            )
        try:
            registry_model = model_by_id(camera.model_id)
        except ValueError as exc:
            self._errors.append(str(exc))
            return
        if camera.model_name != registry_model.name:
            self._errors.append(
                f"CAMERA_ID {camera.camera_id}의 model ID와 이름이 일치하지 않습니다."
            )
        if len(camera.params) != registry_model.num_params:
            self._errors.append(
                f"CAMERA_ID {camera.camera_id}의 파라미터 수가 잘못되었습니다."
            )
        if camera.width <= 0 or camera.height <= 0:
            self._errors.append(
                f"CAMERA_ID {camera.camera_id}의 해상도가 유효하지 않습니다."
            )
        if not all(math.isfinite(value) for value in camera.params):
            self._errors.append(
                f"CAMERA_ID {camera.camera_id}의 파라미터에 유한하지 않은 값이 있습니다."
            )
        if registry_model.exact_blender_projection:
            fx, fy, _, _ = pinhole_parameters(camera.model_name, camera.params)
            if fx <= 0.0 or fy <= 0.0:
                self._errors.append(
                    f"CAMERA_ID {camera.camera_id}의 focal length가 0보다 커야 합니다."
                )
        if not registry_model.exact_blender_projection:
            self._warnings.append(
                f"CAMERA_ID {camera.camera_id} ({camera.model_name})는 원본 파라미터를 "
                "보존하지만 Blender 배경 투영의 정확성을 보장하지 않습니다."
            )

    def _advance_image(self) -> None:
        if self._current_image is None:
            item = self._next_or_phase(
                _ValidationPhase.POINTS3D,
                self._model.points3d.items(),
            )
            if item is None:
                return
            image_key, image = item
            self._current_image = image
            self._current_image_point_index = 0
            if image_key != image.image_id:
                self._errors.append(
                    f"Image dict key {image_key}와 레코드 IMAGE_ID "
                    f"{image.image_id}가 다릅니다."
                )
            if image.camera_id not in self._model.cameras:
                self._errors.append(
                    f"IMAGE_ID {image.image_id}가 존재하지 않는 CAMERA_ID "
                    f"{image.camera_id}를 참조합니다."
                )
            qnorm = math.sqrt(sum(value * value for value in image.qvec))
            if not math.isfinite(qnorm) or qnorm < 1e-12:
                self._errors.append(
                    f"IMAGE_ID {image.image_id}의 quaternion이 유효하지 않습니다."
                )
            if not all(math.isfinite(value) for value in (*image.qvec, *image.tvec)):
                self._errors.append(
                    f"IMAGE_ID {image.image_id}의 pose에 유한하지 않은 값이 있습니다."
                )

        image = self._current_image
        if self._current_image_point_index == len(image.points2d):
            self._current_image = None
            return
        point2d_idx = self._current_image_point_index
        point2d = image.points2d[point2d_idx]
        self._current_image_point_index += 1
        if not all(math.isfinite(value) for value in (point2d.x, point2d.y)):
            self._errors.append(
                f"IMAGE_ID {image.image_id}의 POINT2D_IDX {point2d_idx} "
                "좌표가 유효하지 않습니다."
            )
        if point2d.point3d_id is not None:
            if point2d.point3d_id not in self._model.points3d:
                self._errors.append(
                    f"IMAGE_ID {image.image_id}의 POINT2D_IDX {point2d_idx}가 "
                    f"존재하지 않는 POINT3D_ID {point2d.point3d_id}를 참조합니다."
                )
            self._expected_track_pairs[(image.image_id, point2d_idx)] = (
                point2d.point3d_id
            )

    def _advance_image_batch(self, limit: int) -> None:
        processed = 0
        errors = self._errors
        cameras = self._model.cameras
        points3d = self._model.points3d
        expected_track_pairs = self._expected_track_pairs
        while processed < limit and self._phase is _ValidationPhase.IMAGES:
            if self._current_image is None:
                item = self._next_or_phase(
                    _ValidationPhase.POINTS3D,
                    self._model.points3d.items(),
                )
                if item is None:
                    return
                image_key, image = item
                self._current_image = image
                self._current_image_point_index = 0
                if image_key != image.image_id:
                    errors.append(
                        f"Image dict key {image_key}와 레코드 IMAGE_ID "
                        f"{image.image_id}가 다릅니다."
                    )
                if image.camera_id not in cameras:
                    errors.append(
                        f"IMAGE_ID {image.image_id}가 존재하지 않는 CAMERA_ID "
                        f"{image.camera_id}를 참조합니다."
                    )
                qnorm = math.sqrt(sum(value * value for value in image.qvec))
                if not math.isfinite(qnorm) or qnorm < 1e-12:
                    errors.append(
                        f"IMAGE_ID {image.image_id}의 quaternion이 유효하지 않습니다."
                    )
                if not all(
                    math.isfinite(value)
                    for value in (*image.qvec, *image.tvec)
                ):
                    errors.append(
                        f"IMAGE_ID {image.image_id}의 pose에 유한하지 않은 값이 있습니다."
                    )

            image = self._current_image
            point_count = len(image.points2d)
            if self._current_image_point_index == point_count:
                self._current_image = None
                processed += 1
                continue

            start_index = self._current_image_point_index
            end_index = min(point_count, start_index + limit - processed)
            for point2d_idx in range(start_index, end_index):
                point2d = image.points2d[point2d_idx]
                if not all(
                    math.isfinite(value) for value in (point2d.x, point2d.y)
                ):
                    errors.append(
                        f"IMAGE_ID {image.image_id}의 POINT2D_IDX {point2d_idx} "
                        "좌표가 유효하지 않습니다."
                    )
                if point2d.point3d_id is not None:
                    if point2d.point3d_id not in points3d:
                        errors.append(
                            f"IMAGE_ID {image.image_id}의 POINT2D_IDX "
                            f"{point2d_idx}가 존재하지 않는 POINT3D_ID "
                            f"{point2d.point3d_id}를 참조합니다."
                        )
                    expected_track_pairs[(image.image_id, point2d_idx)] = (
                        point2d.point3d_id
                    )
            completed = end_index - start_index
            self._current_image_point_index = end_index
            processed += completed

    def _advance_point3d(self) -> None:
        if self._current_point is None:
            item = self._next_or_phase(
                _ValidationPhase.EXPECTED_TRACKS,
                self._expected_track_pairs.items(),
            )
            if item is None:
                return
            point_key, point = item
            self._current_point = point
            self._current_track_index = 0
            if point_key != point.point3d_id:
                self._errors.append(
                    f"Point3D dict key {point_key}와 레코드 POINT3D_ID "
                    f"{point.point3d_id}가 다릅니다."
                )
            if not point.track:
                self._errors.append(
                    f"POINT3D_ID {point.point3d_id}의 track이 비어 있습니다."
                )
            if not all(math.isfinite(value) for value in (*point.xyz, point.error)):
                self._errors.append(
                    f"POINT3D_ID {point.point3d_id}의 수치에 유한하지 않은 값이 있습니다."
                )
            if any(channel < 0 or channel > 255 for channel in point.rgb):
                self._errors.append(
                    f"POINT3D_ID {point.point3d_id}의 RGB 범위가 잘못되었습니다."
                )

        point = self._current_point
        if self._current_track_index == len(point.track):
            self._current_point = None
            return
        track = point.track[self._current_track_index]
        self._current_track_index += 1
        pair = (track.image_id, track.point2d_idx)
        if pair in self._actual_track_pairs:
            self._errors.append(f"중복 Point3D track pair입니다: {pair}")
            return
        self._actual_track_pairs[pair] = point.point3d_id
        if self._expected_track_pairs.get(pair) != point.point3d_id:
            self._mismatched_track_pairs.add(pair)
        image = self._model.images.get(track.image_id)
        if image is None:
            self._errors.append(
                f"POINT3D_ID {point.point3d_id}가 존재하지 않는 IMAGE_ID "
                f"{track.image_id}를 참조합니다."
            )
        elif track.point2d_idx < 0 or track.point2d_idx >= len(image.points2d):
            self._errors.append(
                f"POINT3D_ID {point.point3d_id}의 POINT2D_IDX "
                f"{track.point2d_idx}가 IMAGE_ID {track.image_id} 범위를 벗어납니다."
            )

    def _advance_point3d_batch(self, limit: int) -> None:
        processed = 0
        errors = self._errors
        images = self._model.images
        expected_track_pairs = self._expected_track_pairs
        actual_track_pairs = self._actual_track_pairs
        mismatched_track_pairs = self._mismatched_track_pairs
        while processed < limit and self._phase is _ValidationPhase.POINTS3D:
            if self._current_point is None:
                item = self._next_or_phase(
                    _ValidationPhase.EXPECTED_TRACKS,
                    expected_track_pairs.items(),
                )
                if item is None:
                    return
                point_key, point = item
                self._current_point = point
                self._current_track_index = 0
                if point_key != point.point3d_id:
                    errors.append(
                        f"Point3D dict key {point_key}와 레코드 POINT3D_ID "
                        f"{point.point3d_id}가 다릅니다."
                    )
                if not point.track:
                    errors.append(
                        f"POINT3D_ID {point.point3d_id}의 track이 비어 있습니다."
                    )
                if not all(
                    math.isfinite(value)
                    for value in (*point.xyz, point.error)
                ):
                    errors.append(
                        f"POINT3D_ID {point.point3d_id}의 수치에 유한하지 않은 값이 있습니다."
                    )
                if any(channel < 0 or channel > 255 for channel in point.rgb):
                    errors.append(
                        f"POINT3D_ID {point.point3d_id}의 RGB 범위가 잘못되었습니다."
                    )

            point = self._current_point
            track_count = len(point.track)
            if self._current_track_index == track_count:
                self._current_point = None
                processed += 1
                continue

            start_index = self._current_track_index
            end_index = min(track_count, start_index + limit - processed)
            for track_index in range(start_index, end_index):
                track = point.track[track_index]
                pair = (track.image_id, track.point2d_idx)
                if pair in actual_track_pairs:
                    errors.append(f"중복 Point3D track pair입니다: {pair}")
                    continue
                actual_track_pairs[pair] = point.point3d_id
                if expected_track_pairs.get(pair) != point.point3d_id:
                    mismatched_track_pairs.add(pair)
                image = images.get(track.image_id)
                if image is None:
                    errors.append(
                        f"POINT3D_ID {point.point3d_id}가 존재하지 않는 IMAGE_ID "
                        f"{track.image_id}를 참조합니다."
                    )
                elif (
                    track.point2d_idx < 0
                    or track.point2d_idx >= len(image.points2d)
                ):
                    errors.append(
                        f"POINT3D_ID {point.point3d_id}의 POINT2D_IDX "
                        f"{track.point2d_idx}가 IMAGE_ID {track.image_id} "
                        "범위를 벗어납니다."
                    )
            completed = end_index - start_index
            self._current_track_index = end_index
            processed += completed

    def _advance_expected_track(self) -> None:
        try:
            pair, expected = next(self._iterator)
        except StopIteration:
            sorted_pairs = sorted(self._mismatched_track_pairs)
            self._set_phase(
                _ValidationPhase.MISMATCHED_TRACKS,
                sorted_pairs,
            )
            return
        self._phase_completed += 1
        if self._actual_track_pairs.get(pair) != expected:
            self._mismatched_track_pairs.add(pair)

    def _advance_expected_track_batch(self, limit: int) -> None:
        actual_track_pairs = self._actual_track_pairs
        mismatched_track_pairs = self._mismatched_track_pairs
        for _ in range(limit):
            try:
                pair, expected = next(self._iterator)
            except StopIteration:
                self._set_phase(
                    _ValidationPhase.MISMATCHED_TRACKS,
                    sorted(mismatched_track_pairs),
                )
                return
            self._phase_completed += 1
            if actual_track_pairs.get(pair) != expected:
                mismatched_track_pairs.add(pair)

    def _advance_mismatched_track(self) -> None:
        item = self._next_or_phase(
            _ValidationPhase.RIGS,
            self._model.rigs.items(),
        )
        if item is None:
            return
        pair = item
        expected = self._expected_track_pairs.get(pair)
        actual = self._actual_track_pairs.get(pair)
        self._errors.append(
            f"Point2D와 track이 일치하지 않습니다: IMAGE_ID {pair[0]}, "
            f"POINT2D_IDX {pair[1]}, Point2D={expected}, track={actual}"
        )

    def _advance_rig(self) -> None:
        item = self._next_or_phase(
            _ValidationPhase.FRAMES,
            self._model.frames.items(),
        )
        if item is None:
            return
        rig_key, rig = item
        if rig_key != rig.rig_id:
            self._errors.append(
                f"Rig dict key {rig_key}와 레코드 RIG_ID {rig.rig_id}가 다릅니다."
            )
        if rig.rig_id <= 0 or rig.ref_sensor_id <= 0:
            self._errors.append(
                f"RIG_ID {rig.rig_id}의 ID 또는 기준 sensor ID가 유효하지 않습니다."
            )
        non_ref_keys = {
            (sensor.sensor_type, sensor.sensor_id) for sensor in rig.sensors
        }
        if len(non_ref_keys) != len(rig.sensors):
            self._errors.append(f"RIG_ID {rig.rig_id}에 중복 sensor가 있습니다.")
        ref_key = (rig.ref_sensor_type, rig.ref_sensor_id)
        if ref_key in non_ref_keys:
            self._errors.append(
                f"RIG_ID {rig.rig_id}의 기준 sensor가 non-reference 목록에 "
                "중복되었습니다."
            )
        if rig.ref_sensor_type == 0 and rig.ref_sensor_id not in self._model.cameras:
            self._errors.append(
                f"RIG_ID {rig.rig_id}의 기준 CAMERA sensor {rig.ref_sensor_id}가 "
                "Camera 레코드에 없습니다."
            )
        for sensor in rig.sensors:
            if sensor.sensor_id <= 0:
                self._errors.append(
                    f"RIG_ID {rig.rig_id}에 유효하지 않은 sensor ID가 있습니다."
                )
            if sensor.sensor_type == 0 and sensor.sensor_id not in self._model.cameras:
                self._errors.append(
                    f"RIG_ID {rig.rig_id}의 CAMERA sensor {sensor.sensor_id}가 "
                    "Camera 레코드에 없습니다."
                )
            if sensor.has_pose:
                values = (
                    (*sensor.qvec, *sensor.tvec)
                    if sensor.qvec and sensor.tvec
                    else ()
                )
                if len(values) != 7 or not all(
                    math.isfinite(value) for value in values
                ):
                    self._errors.append(
                        f"RIG_ID {rig.rig_id}의 sensor pose가 유효하지 않습니다."
                    )
                elif math.sqrt(
                    sum(value * value for value in sensor.qvec)
                ) < 1e-12:
                    self._errors.append(
                        f"RIG_ID {rig.rig_id}의 sensor quaternion이 유효하지 않습니다."
                    )

    def _advance_frame(self) -> None:
        item = self._next_or_phase(
            _ValidationPhase.MISSING_FRAME_IMAGES,
            self._model.images,
        )
        if item is None:
            return
        frame_key, frame = item
        if frame_key != frame.frame_id:
            self._errors.append(
                f"Frame dict key {frame_key}와 레코드 FRAME_ID "
                f"{frame.frame_id}가 다릅니다."
            )
        if frame.frame_id <= 0 or frame.rig_id <= 0:
            self._errors.append(
                f"FRAME_ID {frame.frame_id}의 ID 또는 RIG_ID가 유효하지 않습니다."
            )
        if not all(math.isfinite(value) for value in (*frame.qvec, *frame.tvec)):
            self._errors.append(
                f"FRAME_ID {frame.frame_id}의 pose에 유한하지 않은 값이 있습니다."
            )
        elif math.sqrt(sum(value * value for value in frame.qvec)) < 1e-12:
            self._errors.append(
                f"FRAME_ID {frame.frame_id}의 quaternion이 유효하지 않습니다."
            )
        rig = self._model.rigs.get(frame.rig_id)
        if rig is None:
            self._errors.append(
                f"FRAME_ID {frame.frame_id}가 존재하지 않는 RIG_ID "
                f"{frame.rig_id}를 참조합니다."
            )
            return
        sensor_keys = {(rig.ref_sensor_type, rig.ref_sensor_id)}
        sensor_keys.update(
            (sensor.sensor_type, sensor.sensor_id) for sensor in rig.sensors
        )
        frame_data_keys: set[tuple[int, int, int]] = set()
        for data_id in frame.data_ids:
            data_key = (data_id.sensor_type, data_id.sensor_id, data_id.data_id)
            if data_key in frame_data_keys:
                self._errors.append(
                    f"FRAME_ID {frame.frame_id}에 중복 DATA_ID가 있습니다: "
                    f"{data_key}"
                )
            frame_data_keys.add(data_key)
            if (data_id.sensor_type, data_id.sensor_id) not in sensor_keys:
                self._errors.append(
                    f"FRAME_ID {frame.frame_id}의 DATA_ID가 RIG_ID "
                    f"{frame.rig_id}에 없는 sensor를 참조합니다."
                )
            if data_id.sensor_type == 0:
                image = self._model.images.get(data_id.data_id)
                if image is None:
                    self._errors.append(
                        f"FRAME_ID {frame.frame_id}가 존재하지 않는 IMAGE_ID "
                        f"{data_id.data_id}를 참조합니다."
                    )
                    continue
                if image.camera_id != data_id.sensor_id:
                    self._errors.append(
                        f"FRAME_ID {frame.frame_id}의 IMAGE_ID {image.image_id}가 "
                        f"CAMERA sensor {data_id.sensor_id}와 다른 CAMERA_ID "
                        f"{image.camera_id}를 사용합니다."
                    )
                previous_frame = self._image_to_frame.get(image.image_id)
                if previous_frame is not None:
                    self._errors.append(
                        f"IMAGE_ID {image.image_id}가 FRAME_ID {previous_frame}와 "
                        f"{frame.frame_id}에 중복 연결되었습니다."
                    )
                else:
                    self._image_to_frame[image.image_id] = frame.frame_id

    def _advance_missing_frame_image(self) -> None:
        try:
            image_id = next(self._iterator)
        except StopIteration:
            self._finish()
            return
        self._phase_completed += 1
        if (
            (self._model.rigs or self._model.frames)
            and image_id not in self._image_to_frame
        ):
            self._errors.append(
                f"IMAGE_ID {image_id}가 어떤 Frame의 CAMERA DATA_ID에도 없습니다."
            )

    def _finish(self) -> None:
        if self._errors:
            preview = "\n".join(f"- {message}" for message in self._errors[:25])
            extra = len(self._errors) - 25
            if extra > 0:
                preview += f"\n- 그 밖의 오류 {extra}개"
            raise ValueError(
                f"COLMAP Reconstruction 무결성 검사에 실패했습니다.\n{preview}"
            )
        self._phase = _ValidationPhase.COMPLETE
        self._phase_completed = 1
        self._phase_total = 1
        self._result = tuple(self._warnings)
