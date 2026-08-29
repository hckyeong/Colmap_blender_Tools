"""Export staging Writer와 parser를 파일 안전 경계로 나누어 실행합니다."""

from __future__ import annotations

import os
import time
import gc
from pathlib import Path
from typing import BinaryIO, Callable, TextIO

from . import binary_io, binary_writer, text_io, text_writer
from .model import Reconstruction


class IncrementalWriteValidationJob:
    """Writer 고유 field 범위 검사를 Point2D·Track 단위로 나눕니다."""

    def __init__(self, model: Reconstruction, *, modern: bool) -> None:
        self._model = model
        self._modern = modern
        self._initialized = False
        self._phase = "cameras"
        self._iterator = None
        self._current = None
        self._inner_index = 0
        self._done = False
        self._completed_units = 0
        self._total_units = max(
            1,
            len(model.cameras)
            + len(model.images)
            + len(model.points3d)
            + len(model.rigs)
            + len(model.frames)
            + sum(len(image.points2d) for image in model.images.values())
            + sum(len(point.track) for point in model.points3d.values()),
        )

    @property
    def done(self) -> bool:
        return self._done

    @property
    def completed_units(self) -> int:
        return self._total_units if self._done else self._completed_units

    @property
    def total_units(self) -> int:
        return self._total_units

    def advance(self, work_budget_ms: float | None) -> None:
        if self._done:
            raise RuntimeError("완료된 Writer field 검사 Job을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        while not self._done:
            batch_phase = self._phase
            if self._phase == "images":
                self._advance_image_batch(2_048)
            elif self._phase == "points3d":
                self._advance_point3d_batch(2_048)
            else:
                for _ in range(2_048):
                    if self._done:
                        break
                    self._advance_one()
                    if self._phase != batch_phase:
                        break
            if deadline is not None and time.perf_counter() >= deadline:
                return

    def _advance_one(self) -> None:
        if not self._initialized:
            self._initialize()
            return
        if self._phase == "cameras":
            self._advance_camera()
        elif self._phase == "images":
            self._advance_image()
        elif self._phase == "points3d":
            self._advance_point3d()
        elif self._phase == "rigs":
            self._advance_rig()
        elif self._phase == "frames":
            self._advance_frame()
        else:
            self._done = True

    def _initialize(self) -> None:
        if not self._modern and (self._model.rigs or self._model.frames):
            raise ValueError(
                "legacy layout으로 Rig 또는 Frame 레코드를 손실 없이 기록할 수 없습니다."
            )
        for label, mapping in (
            ("Camera", self._model.cameras),
            ("Image", self._model.images),
            ("Point3D", self._model.points3d),
            ("Rig", self._model.rigs),
            ("Frame", self._model.frames),
        ):
            text_writer._require_integer(
                len(mapping),
                0,
                text_writer._UINT64_MAX,
                f"{label} 개수",
            )
        self._iterator = iter(self._model.cameras.values())
        self._initialized = True

    def _set_phase(self, phase: str, values) -> None:
        self._phase = phase
        self._iterator = iter(values)
        self._current = None
        self._inner_index = 0

    def _next_or_phase(self, phase: str, values):
        try:
            return next(self._iterator)
        except StopIteration:
            self._set_phase(phase, values)
            return None

    def _advance_camera(self) -> None:
        camera = self._next_or_phase("images", self._model.images.values())
        if camera is None:
            return
        text_writer._validate_model_fields_for_write(
            Reconstruction(cameras={camera.camera_id: camera}),
            modern=True,
        )
        self._completed_units += 1

    def _advance_image(self) -> None:
        if self._current is None:
            image = self._next_or_phase(
                "points3d",
                self._model.points3d.values(),
            )
            if image is None:
                return
            self._current = image
            self._inner_index = 0
            context = f"IMAGE_ID {image.image_id}"
            text_writer._require_integer(
                image.image_id,
                0,
                text_writer._UINT32_MAX,
                f"{context} ID",
            )
            text_writer._require_integer(
                image.camera_id,
                0,
                text_writer._UINT32_MAX,
                f"{context} CAMERA_ID",
            )
            text_writer._require_float_sequence(image.qvec, 4, f"{context} QVEC")
            text_writer._require_float_sequence(image.tvec, 3, f"{context} TVEC")
            text_writer._validate_image_name(image.name, context)
            text_writer._require_integer(
                len(image.points2d),
                0,
                text_writer._UINT64_MAX,
                f"{context} Point2D 개수",
            )
        image = self._current
        if self._inner_index == len(image.points2d):
            self._current = None
            self._completed_units += 1
            return
        point_index = self._inner_index
        point = image.points2d[point_index]
        self._inner_index += 1
        context = f"IMAGE_ID {image.image_id} POINT2D_IDX {point_index}"
        text_writer._require_float_sequence((point.x, point.y), 2, context)
        if point.point3d_id is not None:
            text_writer._require_integer(
                point.point3d_id,
                0,
                text_writer._UINT64_MAX - 1,
                f"{context} POINT3D_ID",
            )
        self._completed_units += 1

    def _advance_image_batch(self, limit: int) -> None:
        require_integer = text_writer._require_integer
        require_float_sequence = text_writer._require_float_sequence
        validate_image_name = text_writer._validate_image_name
        uint32_max = text_writer._UINT32_MAX
        uint64_max = text_writer._UINT64_MAX
        processed = 0
        while processed < limit and self._phase == "images":
            if self._current is None:
                image = self._next_or_phase(
                    "points3d",
                    self._model.points3d.values(),
                )
                if image is None:
                    return
                self._current = image
                self._inner_index = 0
                context = f"IMAGE_ID {image.image_id}"
                require_integer(image.image_id, 0, uint32_max, f"{context} ID")
                require_integer(
                    image.camera_id,
                    0,
                    uint32_max,
                    f"{context} CAMERA_ID",
                )
                require_float_sequence(image.qvec, 4, f"{context} QVEC")
                require_float_sequence(image.tvec, 3, f"{context} TVEC")
                validate_image_name(image.name, context)
                require_integer(
                    len(image.points2d),
                    0,
                    uint64_max,
                    f"{context} Point2D 개수",
                )

            image = self._current
            point_count = len(image.points2d)
            if self._inner_index == point_count:
                self._current = None
                self._completed_units += 1
                processed += 1
                continue

            end_index = min(point_count, self._inner_index + limit - processed)
            for point_index in range(self._inner_index, end_index):
                point = image.points2d[point_index]
                context = (
                    f"IMAGE_ID {image.image_id} POINT2D_IDX {point_index}"
                )
                require_float_sequence((point.x, point.y), 2, context)
                if point.point3d_id is not None:
                    require_integer(
                        point.point3d_id,
                        0,
                        uint64_max - 1,
                        f"{context} POINT3D_ID",
                    )
            completed = end_index - self._inner_index
            self._inner_index = end_index
            self._completed_units += completed
            processed += completed

    def _advance_point3d(self) -> None:
        if self._current is None:
            point = self._next_or_phase("rigs", self._model.rigs.values())
            if point is None:
                return
            self._current = point
            self._inner_index = 0
            context = f"POINT3D_ID {point.point3d_id}"
            text_writer._require_integer(
                point.point3d_id,
                0,
                text_writer._UINT64_MAX - 1,
                f"{context} ID",
            )
            text_writer._require_float_sequence(point.xyz, 3, f"{context} XYZ")
            if len(point.rgb) != 3:
                raise ValueError(f"{context} RGB 값 개수는 3개여야 합니다.")
            for channel_index, channel in enumerate(point.rgb):
                text_writer._require_integer(
                    channel,
                    0,
                    255,
                    f"{context} RGB[{channel_index}]",
                )
            text_writer._require_float_sequence(
                (point.error,),
                1,
                f"{context} ERROR",
            )
            text_writer._require_integer(
                len(point.track),
                0,
                text_writer._UINT64_MAX,
                f"{context} track 개수",
            )
        point = self._current
        if self._inner_index == len(point.track):
            self._current = None
            self._completed_units += 1
            return
        track_index = self._inner_index
        track = point.track[track_index]
        self._inner_index += 1
        context = f"POINT3D_ID {point.point3d_id} TRACK[{track_index}]"
        text_writer._require_integer(
            track.image_id,
            0,
            text_writer._UINT32_MAX,
            f"{context} IMAGE_ID",
        )
        text_writer._require_integer(
            track.point2d_idx,
            0,
            text_writer._UINT32_MAX,
            f"{context} POINT2D_IDX",
        )
        self._completed_units += 1

    def _advance_point3d_batch(self, limit: int) -> None:
        require_integer = text_writer._require_integer
        require_float_sequence = text_writer._require_float_sequence
        uint32_max = text_writer._UINT32_MAX
        uint64_max = text_writer._UINT64_MAX
        processed = 0
        while processed < limit and self._phase == "points3d":
            if self._current is None:
                point = self._next_or_phase("rigs", self._model.rigs.values())
                if point is None:
                    return
                self._current = point
                self._inner_index = 0
                context = f"POINT3D_ID {point.point3d_id}"
                require_integer(
                    point.point3d_id,
                    0,
                    uint64_max - 1,
                    f"{context} ID",
                )
                require_float_sequence(point.xyz, 3, f"{context} XYZ")
                if len(point.rgb) != 3:
                    raise ValueError(
                        f"{context} RGB 값 개수는 3개여야 합니다."
                    )
                for channel_index, channel in enumerate(point.rgb):
                    require_integer(
                        channel,
                        0,
                        255,
                        f"{context} RGB[{channel_index}]",
                    )
                require_float_sequence(
                    (point.error,),
                    1,
                    f"{context} ERROR",
                )
                require_integer(
                    len(point.track),
                    0,
                    uint64_max,
                    f"{context} track 개수",
                )

            point = self._current
            track_count = len(point.track)
            if self._inner_index == track_count:
                self._current = None
                self._completed_units += 1
                processed += 1
                continue

            end_index = min(track_count, self._inner_index + limit - processed)
            for track_index in range(self._inner_index, end_index):
                track = point.track[track_index]
                context = (
                    f"POINT3D_ID {point.point3d_id} TRACK[{track_index}]"
                )
                require_integer(
                    track.image_id,
                    0,
                    uint32_max,
                    f"{context} IMAGE_ID",
                )
                require_integer(
                    track.point2d_idx,
                    0,
                    uint32_max,
                    f"{context} POINT2D_IDX",
                )
            completed = end_index - self._inner_index
            self._inner_index = end_index
            self._completed_units += completed
            processed += completed

    def _advance_rig(self) -> None:
        rig = self._next_or_phase("frames", self._model.frames.values())
        if rig is None:
            return
        text_writer._validate_model_fields_for_write(
            Reconstruction(rigs={rig.rig_id: rig}),
            modern=True,
        )
        self._completed_units += 1

    def _advance_frame(self) -> None:
        try:
            frame = next(self._iterator)
        except StopIteration:
            self._phase = "complete"
            self._done = True
            return
        text_writer._validate_model_fields_for_write(
            Reconstruction(frames={frame.frame_id: frame}),
            modern=True,
        )
        self._completed_units += 1


class StagingWriteJob:
    """기존 Writer의 파일별 함수를 순서대로 실행하고 각각 `fsync`합니다."""

    def __init__(
        self,
        directory: Path,
        model: Reconstruction,
        *,
        input_format: str,
        modern: bool,
    ) -> None:
        self._directory = directory
        self._model = model
        self._input_format = input_format
        self._modern = modern
        self._validated = False
        self._validation_job = IncrementalWriteValidationJob(
            model,
            modern=modern,
        )
        self._index = 0
        self._tasks = self._build_tasks()
        self._binary_stream: BinaryIO | None = None
        self._binary_iterator = None
        self._binary_current = None
        self._binary_inner_index = 0
        self._text_stream: TextIO | None = None
        self._text_iterator = None
        self._text_current = None
        self._text_inner_index = 0
        self._completed_work = 0
        self._total_work = max(
            1,
            len(model.cameras)
            + len(model.images)
            + len(model.points3d)
            + len(model.rigs)
            + len(model.frames)
            + sum(len(image.points2d) for image in model.images.values())
            + sum(len(point.track) for point in model.points3d.values()),
        )

    @property
    def done(self) -> bool:
        return self._validated and self._index == len(self._tasks)

    @property
    def completed_units(self) -> int:
        if not self._validated:
            return self._validation_job.completed_units
        return self._validation_job.total_units + self._completed_work

    @property
    def total_units(self) -> int:
        return self._validation_job.total_units + self._total_work

    def advance(self, work_budget_ms: float | None = None) -> None:
        if self.done:
            raise RuntimeError("완료된 Staging Writer Job을 다시 실행할 수 없습니다.")
        if not self._validated:
            if not self._directory.is_dir():
                label = "BIN" if self._input_format == "binary" else "TXT"
                raise ValueError(
                    f"{label} Export 대상 디렉터리가 존재하지 않습니다: "
                    f"{self._directory}"
                )
            self._validation_job.advance(work_budget_ms)
            if self._validation_job.done:
                self._validated = True
            return
        if self._input_format == "binary":
            self._advance_binary(work_budget_ms)
        else:
            path, writer = self._tasks[self._index]
            if path.stem in {"images", "points3D"}:
                self._advance_text_variable(path, work_budget_ms)
                return
            writer(path, self._model)
            with path.open("rb+") as stream:
                os.fsync(stream.fileno())
            self._completed_work += self._work_for_stem(path.stem)
            self._index += 1

    def close(self) -> None:
        if self._binary_stream is not None:
            self._binary_stream.close()
            self._binary_stream = None
        if self._text_stream is not None:
            self._text_stream.close()
            self._text_stream = None

    def _advance_binary(self, work_budget_ms: float | None) -> None:
        path, writer = self._tasks[self._index]
        if path.stem not in {"images", "points3D"}:
            writer(path, self._model)
            with path.open("rb+") as stream:
                os.fsync(stream.fileno())
            self._completed_work += self._work_for_stem(path.stem)
            self._index += 1
            return
        if self._binary_stream is None:
            self._binary_stream = path.open(
                "wb",
                buffering=binary_writer._WRITE_BUFFER_BYTES,
            )
            if path.stem == "images":
                values = self._model.images.values()
            else:
                values = self._model.points3d.values()
            self._binary_iterator = iter(values)
            self._binary_stream.write(binary_writer._COUNT_STRUCT.pack(len(values)))

        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        processed = 0
        while True:
            if self._binary_current is None:
                try:
                    self._binary_current = next(self._binary_iterator)
                except StopIteration:
                    self._finish_binary_file()
                    return
                self._binary_inner_index = 0
                self._write_binary_outer_header(path.stem)

            if path.stem == "images":
                records = self._binary_current.points2d
                pack = binary_writer._POINT2D_STRUCT.pack
                while self._binary_inner_index < len(records):
                    point = records[self._binary_inner_index]
                    self._binary_stream.write(
                        pack(
                            point.x,
                            point.y,
                            binary_writer._POINT3D_SENTINEL
                            if point.point3d_id is None
                            else point.point3d_id,
                        )
                    )
                    self._binary_inner_index += 1
                    self._completed_work += 1
                    processed += 1
                    if processed >= 2_048:
                        if self._budget_expired(deadline, processed):
                            return
                        processed = 0
            else:
                records = self._binary_current.track
                pack = binary_writer._TRACK_STRUCT.pack
                while self._binary_inner_index < len(records):
                    track = records[self._binary_inner_index]
                    self._binary_stream.write(
                        pack(track.image_id, track.point2d_idx)
                    )
                    self._binary_inner_index += 1
                    self._completed_work += 1
                    processed += 1
                    if processed >= 2_048:
                        if self._budget_expired(deadline, processed):
                            return
                        processed = 0
            self._binary_current = None
            self._completed_work += 1
            processed += 1
            if processed >= 2_048:
                if self._budget_expired(deadline, processed):
                    return
                processed = 0

    @staticmethod
    def _budget_expired(deadline: float | None, processed: int) -> bool:
        return (
            deadline is not None
            and processed > 0
            and time.perf_counter() >= deadline
        )

    def _write_binary_outer_header(self, stem: str) -> None:
        if self._binary_stream is None or self._binary_current is None:
            raise RuntimeError("Binary staging Writer 상태가 준비되지 않았습니다.")
        write = self._binary_stream.write
        if stem == "images":
            image = self._binary_current
            write(
                binary_writer._IMAGE_HEADER_STRUCT.pack(
                    image.image_id,
                    *image.qvec,
                    *image.tvec,
                    image.camera_id,
                )
            )
            write(image.name.encode("utf-8", errors="strict"))
            write(b"\0")
            write(binary_writer._COUNT_STRUCT.pack(len(image.points2d)))
        else:
            point = self._binary_current
            write(
                binary_writer._POINT3D_HEADER_STRUCT.pack(
                    point.point3d_id,
                    *point.xyz,
                    *point.rgb,
                    point.error,
                )
            )
            write(binary_writer._COUNT_STRUCT.pack(len(point.track)))

    def _finish_binary_file(self) -> None:
        if self._binary_stream is None:
            raise RuntimeError("완료할 Binary staging stream이 없습니다.")
        self._binary_stream.flush()
        os.fsync(self._binary_stream.fileno())
        self._binary_stream.close()
        self._binary_stream = None
        self._binary_iterator = None
        self._binary_current = None
        self._binary_inner_index = 0
        self._index += 1

    def _advance_text_variable(
        self,
        path: Path,
        work_budget_ms: float | None,
    ) -> None:
        if self._text_stream is None:
            self._text_stream = text_writer._open_text(path)
            if path.stem == "images":
                self._text_stream.write(
                    "# Image list with two lines per image: "
                    "IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n"
                )
                self._text_stream.write("# POINTS2D[] as (X, Y, POINT3D_ID)\n")
                self._text_stream.write(
                    f"# Number of images: {len(self._model.images)}\n"
                )
                self._text_iterator = iter(self._model.images.values())
            else:
                self._text_stream.write(
                    "# 3D point list: POINT3D_ID, X, Y, Z, R, G, B, ERROR, "
                    "TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
                )
                self._text_stream.write(
                    f"# Number of points: {len(self._model.points3d)}\n"
                )
                self._text_iterator = iter(self._model.points3d.values())

        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        processed = 0
        while True:
            if self._text_current is None:
                try:
                    self._text_current = next(self._text_iterator)
                except StopIteration:
                    self._finish_text_file()
                    return
                self._text_inner_index = 0
                self._write_text_outer_header(path.stem)

            if path.stem == "images":
                records = self._text_current.points2d
                while self._text_inner_index < len(records):
                    point = records[self._text_inner_index]
                    prefix = "" if self._text_inner_index == 0 else " "
                    self._text_stream.write(
                        prefix
                        + " ".join(
                            (
                                text_writer._format_float(point.x),
                                text_writer._format_float(point.y),
                                "-1"
                                if point.point3d_id is None
                                else str(point.point3d_id),
                            )
                        )
                    )
                    self._text_inner_index += 1
                    self._completed_work += 1
                    processed += 1
                    if processed >= 2_048:
                        if self._budget_expired(deadline, processed):
                            return
                        processed = 0
            else:
                records = self._text_current.track
                while self._text_inner_index < len(records):
                    track = records[self._text_inner_index]
                    self._text_stream.write(
                        f" {track.image_id} {track.point2d_idx}"
                    )
                    self._text_inner_index += 1
                    self._completed_work += 1
                    processed += 1
                    if processed >= 2_048:
                        if self._budget_expired(deadline, processed):
                            return
                        processed = 0
            self._text_stream.write("\n")
            self._text_current = None
            self._completed_work += 1
            processed += 1
            if processed >= 2_048:
                if self._budget_expired(deadline, processed):
                    return
                processed = 0

    def _write_text_outer_header(self, stem: str) -> None:
        if self._text_stream is None or self._text_current is None:
            raise RuntimeError("Text staging Writer 상태가 준비되지 않았습니다.")
        if stem == "images":
            image = self._text_current
            header = [
                str(image.image_id),
                *(text_writer._format_float(value) for value in image.qvec),
                *(text_writer._format_float(value) for value in image.tvec),
                str(image.camera_id),
                image.name,
            ]
            self._text_stream.write(" ".join(header) + "\n")
        else:
            point = self._text_current
            values = [
                str(point.point3d_id),
                *(text_writer._format_float(value) for value in point.xyz),
                *(str(value) for value in point.rgb),
                text_writer._format_float(point.error),
            ]
            self._text_stream.write(" ".join(values))

    def _finish_text_file(self) -> None:
        if self._text_stream is None:
            raise RuntimeError("완료할 Text staging stream이 없습니다.")
        self._text_stream.flush()
        os.fsync(self._text_stream.fileno())
        self._text_stream.close()
        self._text_stream = None
        self._text_iterator = None
        self._text_current = None
        self._text_inner_index = 0
        self._index += 1

    def _work_for_stem(self, stem: str) -> int:
        mapping = {
            "rigs": self._model.rigs,
            "cameras": self._model.cameras,
            "frames": self._model.frames,
            "images": self._model.images,
            "points3D": self._model.points3d,
        }[stem]
        if stem == "images":
            return len(mapping) + sum(len(image.points2d) for image in mapping.values())
        if stem == "points3D":
            return len(mapping) + sum(len(point.track) for point in mapping.values())
        return len(mapping)

    def _build_tasks(
        self,
    ) -> tuple[tuple[Path, Callable[[Path, Reconstruction], None]], ...]:
        if self._input_format == "binary":
            module = binary_writer
            extension = "bin"
        elif self._input_format == "text":
            module = text_writer
            extension = "txt"
        else:
            raise ValueError(f"지원하지 않는 Export 형식입니다: {self._input_format}")

        tasks: list[tuple[Path, Callable[[Path, Reconstruction], None]]] = []
        if self._modern:
            tasks.append((self._directory / f"rigs.{extension}", module._write_rigs))
        tasks.append(
            (self._directory / f"cameras.{extension}", module._write_cameras)
        )
        if self._modern:
            tasks.append(
                (self._directory / f"frames.{extension}", module._write_frames)
            )
        tasks.extend(
            (
                (self._directory / f"images.{extension}", module._write_images),
                (
                    self._directory / f"points3D.{extension}",
                    module._write_points3d,
                ),
            )
        )
        return tuple(tasks)


class StagingReadJob:
    """COLMAP staging 파일을 기존 parser로 파일별 해석합니다."""

    def __init__(
        self,
        directory: Path,
        *,
        input_format: str,
        modern: bool,
    ) -> None:
        self._directory = directory
        self._input_format = input_format
        self._modern = modern
        self._index = 0
        self._values: dict[str, dict] = {}
        self._tasks = self._build_tasks()
        self._result: Reconstruction | None = None
        self._cursor_context = None
        self._cursor = None
        self._record_count = 0
        self._record_index = 0
        self._records: dict = {}
        self._current_header = None
        self._current_name: str | None = None
        self._current_inner_count = 0
        self._current_inner: list = []
        self._completed_file_bytes = 0
        self._total_file_bytes = sum(path.stat().st_size for _, path, _ in self._tasks)
        self._text_stream: TextIO | None = None
        self._text_records: dict = {}

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def completed_units(self) -> int:
        if self._cursor is not None:
            current = self._cursor.offset
        elif self._text_stream is not None:
            current = min(
                self._tasks[self._index][1].stat().st_size,
                self._text_stream.tell(),
            )
        else:
            current = 0
        return self._completed_file_bytes + current

    @property
    def total_units(self) -> int:
        return self._total_file_bytes

    @property
    def result(self) -> Reconstruction:
        if self._result is None:
            raise RuntimeError("완료되지 않은 Staging Parser Job의 결과를 읽을 수 없습니다.")
        return self._result

    def advance(self, work_budget_ms: float | None = None) -> None:
        if self.done:
            raise RuntimeError("완료된 Staging Parser Job을 다시 실행할 수 없습니다.")
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            field_name, path, reader = self._tasks[self._index]
            if self._input_format == "binary" and field_name in {"images", "points3d"}:
                self._advance_binary_variable(field_name, path, work_budget_ms)
            elif self._input_format == "text" and field_name in {"images", "points3d"}:
                self._advance_text_variable(field_name, path, work_budget_ms)
            else:
                self._values[field_name] = reader(path)
                self._completed_file_bytes += path.stat().st_size
                self._finish_task()
        finally:
            if gc_was_enabled:
                gc.enable()
                # 자동 full collection 대신 이번 tick에서 생성한 세대만 정리합니다.
                gc.collect(0)

    def close(self) -> None:
        if self._cursor_context is not None:
            self._cursor_context.__exit__(None, None, None)
            self._cursor_context = None
            self._cursor = None
        if self._text_stream is not None:
            self._text_stream.close()
            self._text_stream = None

    def _advance_binary_variable(
        self,
        field_name: str,
        path: Path,
        work_budget_ms: float | None,
    ) -> None:
        if self._cursor is None:
            self._cursor_context = binary_io._open_cursor(path)
            self._cursor = self._cursor_context.__enter__()
            (self._record_count,) = self._cursor.unpack_one(
                binary_io._COUNT_STRUCT,
                "Image 개수" if field_name == "images" else "Point3D 개수",
            )
            header_struct = (
                binary_io._IMAGE_HEADER_STRUCT
                if field_name == "images"
                else binary_io._POINT3D_HEADER_STRUCT
            )
            self._cursor.ensure_count_fits(
                self._record_count,
                header_struct.size,
                "Image 레코드" if field_name == "images" else "Point3D 레코드",
            )
            self._record_index = 0
            self._records = {}

        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        processed = 0
        while self._record_index < self._record_count:
            if self._current_header is None:
                self._start_binary_record(field_name)
            inner_struct = (
                binary_io._POINT2D_STRUCT
                if field_name == "images"
                else binary_io._TRACK_STRUCT
            )
            factory = (
                binary_io._point2d_record
                if field_name == "images"
                else binary_io._track_element
            )
            while len(self._current_inner) < self._current_inner_count:
                batch_count = min(
                    16_384,
                    self._current_inner_count - len(self._current_inner),
                )
                self._current_inner.extend(
                    self._cursor.map_fixed(
                        inner_struct,
                        batch_count,
                        self._binary_inner_context(field_name),
                        factory,
                    )
                )
                processed += batch_count
                if StagingWriteJob._budget_expired(deadline, processed):
                    return
            self._finish_binary_record(field_name)
            processed += 1
            if StagingWriteJob._budget_expired(deadline, processed):
                return

        self._cursor.ensure_eof()
        self._values[field_name] = self._records
        self._completed_file_bytes += path.stat().st_size
        self.close()
        self._finish_task()

    def _start_binary_record(self, field_name: str) -> None:
        if field_name == "images":
            values = self._cursor.unpack_one(
                binary_io._IMAGE_HEADER_STRUCT,
                "Image 헤더",
            )
            image_id = values[0]
            self._current_name = self._cursor.read_c_string(
                f"IMAGE_ID {image_id} 이름"
            )
            (inner_count,) = self._cursor.unpack_one(
                binary_io._COUNT_STRUCT,
                f"IMAGE_ID {image_id} Point2D 개수",
            )
            self._cursor.ensure_count_fits(
                inner_count,
                binary_io._POINT2D_STRUCT.size,
                f"IMAGE_ID {image_id} Point2D",
            )
        else:
            values = self._cursor.unpack_one(
                binary_io._POINT3D_HEADER_STRUCT,
                "Point3D 헤더",
            )
            point3d_id = values[0]
            (inner_count,) = self._cursor.unpack_one(
                binary_io._TRACK_COUNT_STRUCT,
                f"POINT3D_ID {point3d_id} track 개수",
            )
            self._cursor.ensure_count_fits(
                inner_count,
                binary_io._TRACK_STRUCT.size,
                f"POINT3D_ID {point3d_id} track",
            )
        self._current_header = values
        self._current_inner_count = inner_count
        self._current_inner = []

    def _binary_inner_context(self, field_name: str) -> str:
        record_id = self._current_header[0]
        if field_name == "images":
            return f"IMAGE_ID {record_id} Point2D"
        return f"POINT3D_ID {record_id} track"

    def _finish_binary_record(self, field_name: str) -> None:
        values = self._current_header
        record_id = values[0]
        if record_id in self._records:
            label = "IMAGE_ID" if field_name == "images" else "POINT3D_ID"
            raise ValueError(f"중복 {label}입니다: {record_id}")
        if field_name == "images":
            from .model import ImageRecord

            self._records[record_id] = ImageRecord(
                image_id=record_id,
                qvec=tuple(values[1:5]),
                tvec=tuple(values[5:8]),
                camera_id=values[8],
                name=self._current_name or "",
                points2d=tuple(self._current_inner),
            )
        else:
            from .model import Point3DRecord

            self._records[record_id] = Point3DRecord(
                point3d_id=record_id,
                xyz=tuple(values[1:4]),
                rgb=tuple(values[4:7]),
                error=values[7],
                track=tuple(self._current_inner),
            )
        self._record_index += 1
        self._current_header = None
        self._current_name = None
        self._current_inner_count = 0
        self._current_inner = []

    def _finish_task(self) -> None:
        self._index += 1
        if self._index == len(self._tasks):
            self._result = Reconstruction(
                cameras=self._values["cameras"],
                images=self._values["images"],
                points3d=self._values["points3d"],
                rigs=self._values.get("rigs", {}),
                frames=self._values.get("frames", {}),
            )

    def _advance_text_variable(
        self,
        field_name: str,
        path: Path,
        work_budget_ms: float | None,
    ) -> None:
        if self._text_stream is None:
            self._text_stream = path.open("r", encoding="utf-8-sig")
            self._text_records = {}
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        processed = 0
        while True:
            line = self._next_text_data_line()
            if line is None:
                self._values[field_name] = self._text_records
                self._completed_file_bytes += path.stat().st_size
                self._text_stream.close()
                self._text_stream = None
                self._text_records = {}
                self._finish_task()
                return
            if field_name == "images":
                self._parse_text_image(path, line)
            else:
                self._parse_text_point3d(path, line)
            processed += 1
            if StagingWriteJob._budget_expired(deadline, processed):
                return

    def _next_text_data_line(self) -> str | None:
        while True:
            physical = self._text_stream.readline()
            if physical == "":
                return None
            line = physical.strip()
            if line and not line.startswith("#"):
                return line

    def _parse_text_image(self, path: Path, header: str) -> None:
        fields = header.split(maxsplit=9)
        if len(fields) != 10:
            raise ValueError(f"{path}의 image 헤더가 불완전합니다: {header}")
        points_physical = self._text_stream.readline()
        if points_physical == "":
            raise ValueError(f"IMAGE_ID {fields[0]}의 Point2D 행이 누락되었습니다.")
        point_values = points_physical.strip().split()
        if len(point_values) % 3:
            raise ValueError(
                f"IMAGE_ID {fields[0]}의 Point2D 값 개수가 3의 배수가 아닙니다."
            )
        from .model import ImageRecord, Point2DRecord

        points = []
        for offset in range(0, len(point_values), 3):
            point3d_value = int(point_values[offset + 2])
            points.append(
                Point2DRecord(
                    float(point_values[offset]),
                    float(point_values[offset + 1]),
                    None if point3d_value == -1 else point3d_value,
                )
            )
        image_id = int(fields[0])
        if image_id in self._text_records:
            raise ValueError(f"중복 IMAGE_ID입니다: {image_id}")
        self._text_records[image_id] = ImageRecord(
            image_id=image_id,
            qvec=tuple(float(value) for value in fields[1:5]),
            tvec=tuple(float(value) for value in fields[5:8]),
            camera_id=int(fields[8]),
            name=fields[9],
            points2d=tuple(points),
        )

    def _parse_text_point3d(self, path: Path, line: str) -> None:
        values = line.split()
        if len(values) < 8 or (len(values) - 8) % 2:
            raise ValueError(f"{path}의 Point3D 레코드가 불완전합니다: {line}")
        from .model import Point3DRecord, TrackElement

        point3d_id = int(values[0])
        track = tuple(
            TrackElement(int(values[offset]), int(values[offset + 1]))
            for offset in range(8, len(values), 2)
        )
        if point3d_id in self._text_records:
            raise ValueError(f"중복 POINT3D_ID입니다: {point3d_id}")
        self._text_records[point3d_id] = Point3DRecord(
            point3d_id=point3d_id,
            xyz=tuple(float(value) for value in values[1:4]),
            rgb=tuple(int(value) for value in values[4:7]),
            error=float(values[7]),
            track=track,
        )

    def _build_tasks(
        self,
    ) -> tuple[tuple[str, Path, Callable[[Path], dict]], ...]:
        extension: str
        if self._input_format == "binary":
            extension = "bin"
            readers = {
                "cameras": binary_io.read_cameras_binary,
                "images": binary_io.read_images_binary,
                "points3d": binary_io.read_points3d_binary,
                "rigs": binary_io.read_rigs_binary,
                "frames": binary_io.read_frames_binary,
            }
        elif self._input_format == "text":
            extension = "txt"
            readers = {
                "cameras": text_io.read_cameras_text,
                "images": text_io.read_images_text,
                "points3d": text_io.read_points3d_text,
                "rigs": text_io.read_rigs_text,
                "frames": text_io.read_frames_text,
            }
        else:
            raise ValueError(f"지원하지 않는 Export 형식입니다: {self._input_format}")

        order = ["cameras", "images", "points3d"]
        if self._modern:
            order.extend(("rigs", "frames"))
        return tuple(
            (
                field_name,
                self._directory
                / f"{'points3D' if field_name == 'points3d' else field_name}.{extension}",
                readers[field_name],
            )
            for field_name in order
        )
