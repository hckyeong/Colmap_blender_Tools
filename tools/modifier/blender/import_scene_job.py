"""검증된 Reconstruction을 Blender staging으로 증분 변환합니다."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from enum import Enum, auto
from math import isfinite
from time import perf_counter

from ..core.import_types import (
    CAMERA_FRUSTUM_SIZE_MAX,
    CAMERA_FRUSTUM_SIZE_MIN,
    DEFAULT_CAMERA_FRUSTUM_SIZE,
)
from ..core.model import LoadedReconstruction
from .scene_elements import (
    _create_axis_root,
    _create_camera,
    _create_point_color_attribute,
    _create_point_error_attribute,
    _create_point_id_attributes,
    _create_point_index_attribute,
    _create_point_mesh,
    _create_point_object_display,
    _create_point_pending_attribute,
    _signed_int32,
    create_staging_collection_tree,
)
from .scene_transaction import StagedScene, cleanup_staging


_CAMERA_BATCH_SIZE = 16


class ImportScenePhase(Enum):
    """Import staging의 사용자 표시 가능한 단계입니다."""

    CREATED = auto()
    CREATE_COLLECTION_TREE = auto()
    PREPARE_POINT_BUFFERS = auto()
    CREATE_POINT_MESH = auto()
    CREATE_POINT_ATTRIBUTES = auto()
    CREATE_POINT_DISPLAY = auto()
    CREATE_CAMERAS = auto()
    READY_TO_COMMIT = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class ImportSceneProgressSnapshot:
    """Scene Job의 현재 단계와 실제 처리량입니다."""

    phase: ImportScenePhase
    overall_fraction: float
    completed_units: int
    total_units: int


class ImportSceneBuildJob:
    """하나의 Point Mesh와 Image별 Camera를 staging에 단계적으로 생성합니다."""

    def __init__(
        self,
        loaded: LoadedReconstruction,
        *,
        point_size: float,
        forward_axis: str,
        up_axis: str,
        camera_frustum_size: float = DEFAULT_CAMERA_FRUSTUM_SIZE,
        point_id_order: tuple[int, ...] | None = None,
    ) -> None:
        if not isfinite(point_size) or point_size <= 0.0:
            raise ValueError("Point Scale은 0보다 큰 유한한 값이어야 합니다.")
        if (
            not isfinite(camera_frustum_size)
            or camera_frustum_size < CAMERA_FRUSTUM_SIZE_MIN
            or camera_frustum_size > CAMERA_FRUSTUM_SIZE_MAX
        ):
            raise ValueError(
                "Camera Frustum Size는 0.01 이상 1000.0 이하의 "
                "유한한 값이어야 합니다."
            )
        self._loaded = loaded
        self._point_size = float(point_size)
        self._camera_frustum_size = float(camera_frustum_size)
        self._forward_axis = forward_axis
        self._up_axis = up_axis
        self._phase = ImportScenePhase.CREATED
        self._error: Exception | None = None
        self._staged: StagedScene | None = None
        self._points_collection = None
        self._cameras_collection = None
        self._guides_collection = None
        self._axis_root = None
        self._mesh = None
        self._point_ids = (
            tuple(sorted(loaded.reconstruction.points3d))
            if point_id_order is None
            else point_id_order
        )
        if len(self._point_ids) != len(loaded.reconstruction.points3d):
            raise ValueError("Scene Job의 Point3D 순서표와 모델 레코드 수가 다릅니다.")
        self._point_cursor = 0
        self._coordinates: list[tuple[float, float, float]] = []
        self._colors = array("f")
        self._errors = array("f")
        self._point3d_id_low = array("i")
        self._point3d_id_high = array("i")
        self._attribute_step = 0
        self._image_records = ()
        self._image_cursor = 0
        self._fixed_completed = 0
        self._total_units = (
            len(loaded.reconstruction.points3d)
            + len(loaded.reconstruction.images)
            + 9
        )

    @property
    def done(self) -> bool:
        return self._phase is ImportScenePhase.READY_TO_COMMIT

    @property
    def failed(self) -> bool:
        return self._phase is ImportScenePhase.FAILED

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def result(self) -> StagedScene:
        if not self.done or self._staged is None:
            raise RuntimeError("완료되지 않은 Scene Job의 staging 결과를 읽을 수 없습니다.")
        return self._staged

    @property
    def progress(self) -> ImportSceneProgressSnapshot:
        completed = (
            self._point_cursor
            + self._image_cursor
            + self._fixed_completed
        )
        fraction = (
            1.0
            if self.done
            else min(completed / max(1, self._total_units), 0.999)
        )
        return ImportSceneProgressSnapshot(
            phase=self._phase,
            overall_fraction=fraction,
            completed_units=completed,
            total_units=self._total_units,
        )

    def _create_collection_tree(self) -> None:
        self._phase = ImportScenePhase.CREATE_COLLECTION_TREE
        (
            root,
            self._points_collection,
            self._cameras_collection,
            self._guides_collection,
        ) = create_staging_collection_tree(self._loaded)
        self._staged = StagedScene(root)
        self._axis_root = _create_axis_root(
            self._loaded,
            self._guides_collection,
            self._forward_axis,
            self._up_axis,
        )
        self._image_records = tuple(
            sorted(
                self._loaded.reconstruction.images.values(),
                key=lambda item: item.image_id,
            )
        )
        self._fixed_completed += 1
        self._phase = ImportScenePhase.PREPARE_POINT_BUFFERS

    def _prepare_point_buffers(self, deadline: float | None) -> None:
        while self._point_cursor < len(self._point_ids):
            point_id = self._point_ids[self._point_cursor]
            try:
                point = self._loaded.reconstruction.points3d[point_id]
            except KeyError as exc:
                raise ValueError(
                    f"Scene Job의 Point3D 순서표에 없는 ID가 있습니다: {point_id}"
                ) from exc
            self._coordinates.append(point.xyz)
            self._colors.extend(
                (
                    point.rgb[0] / 255.0,
                    point.rgb[1] / 255.0,
                    point.rgb[2] / 255.0,
                    1.0,
                )
            )
            self._errors.append(point.error)
            self._point3d_id_low.append(_signed_int32(point.point3d_id))
            self._point3d_id_high.append(_signed_int32(point.point3d_id >> 32))
            self._point_cursor += 1
            if (
                self._point_cursor % 4096 == 0
                and deadline is not None
                and perf_counter() >= deadline
            ):
                return
        self._point_ids = ()
        self._phase = ImportScenePhase.CREATE_POINT_MESH

    def _create_mesh(self) -> None:
        self._mesh = _create_point_mesh(self._loaded, self._coordinates)
        self._coordinates.clear()
        self._fixed_completed += 1
        self._phase = ImportScenePhase.CREATE_POINT_ATTRIBUTES

    def _create_next_attribute(self) -> None:
        if self._mesh is None:
            raise RuntimeError("Point Attribute보다 먼저 Mesh가 생성되지 않았습니다.")
        if self._attribute_step == 0:
            _create_point_color_attribute(self._mesh, self._colors)
            del self._colors[:]
        elif self._attribute_step == 1:
            _create_point_error_attribute(self._mesh, self._errors)
            del self._errors[:]
        elif self._attribute_step == 2:
            _create_point_index_attribute(self._mesh)
        elif self._attribute_step == 3:
            _create_point_id_attributes(
                self._mesh,
                self._point3d_id_low,
                self._point3d_id_high,
            )
            del self._point3d_id_low[:]
            del self._point3d_id_high[:]
        elif self._attribute_step == 4:
            _create_point_pending_attribute(self._mesh)
        else:
            self._phase = ImportScenePhase.CREATE_POINT_DISPLAY
            return
        self._attribute_step += 1
        self._fixed_completed += 1
        if self._attribute_step == 5:
            self._phase = ImportScenePhase.CREATE_POINT_DISPLAY

    def _create_display(self) -> None:
        if self._mesh is None or self._axis_root is None:
            raise RuntimeError("Point 표시보다 먼저 Mesh와 Axis Root가 생성되지 않았습니다.")
        _create_point_object_display(
            self._loaded,
            self._points_collection,
            self._axis_root,
            self._mesh,
            self._point_size,
        )
        self._fixed_completed += 1
        self._phase = ImportScenePhase.CREATE_CAMERAS

    def _create_camera_batch(self, deadline: float | None) -> None:
        batch_count = 0
        while self._image_cursor < len(self._image_records):
            image = self._image_records[self._image_cursor]
            _create_camera(
                self._loaded,
                self._cameras_collection,
                self._axis_root,
                image,
                camera_frustum_size=self._camera_frustum_size,
            )
            self._image_cursor += 1
            batch_count += 1
            if batch_count >= _CAMERA_BATCH_SIZE:
                return
            if deadline is not None and perf_counter() >= deadline:
                return
        self._image_records = ()
        self._fixed_completed += 1
        self._phase = ImportScenePhase.READY_TO_COMMIT

    def advance(
        self,
        *,
        work_budget_ms: float | None = None,
    ) -> ImportSceneProgressSnapshot:
        """현재 단계에서 budget 내 작업을 수행하고 최신 snapshot을 반환합니다."""

        if self.done:
            raise RuntimeError("완료된 Scene Job을 다시 실행할 수 없습니다.")
        if self.failed:
            raise RuntimeError("실패한 Scene Job을 다시 실행할 수 없습니다.")
        if work_budget_ms is not None and (
            not isfinite(work_budget_ms) or work_budget_ms <= 0.0
        ):
            raise ValueError("Scene Job work budget은 0보다 큰 유한한 값이어야 합니다.")
        deadline = (
            None
            if work_budget_ms is None
            else perf_counter() + work_budget_ms / 1000.0
        )
        try:
            while not self.done:
                phase_before = self._phase
                if self._phase is ImportScenePhase.CREATED:
                    self._create_collection_tree()
                elif self._phase is ImportScenePhase.PREPARE_POINT_BUFFERS:
                    self._prepare_point_buffers(deadline)
                elif self._phase is ImportScenePhase.CREATE_POINT_MESH:
                    self._create_mesh()
                elif self._phase is ImportScenePhase.CREATE_POINT_ATTRIBUTES:
                    self._create_next_attribute()
                elif self._phase is ImportScenePhase.CREATE_POINT_DISPLAY:
                    self._create_display()
                elif self._phase is ImportScenePhase.CREATE_CAMERAS:
                    self._create_camera_batch(deadline)
                else:
                    raise RuntimeError(f"지원하지 않는 Scene Job 단계입니다: {self._phase}")

                # Blender API 한 번이 budget을 넘을 수 있으므로 각 단계 경계에서
                # 반드시 Modal loop로 제어권을 반환합니다.
                if deadline is not None and perf_counter() >= deadline:
                    break
                if (
                    phase_before is ImportScenePhase.CREATE_POINT_ATTRIBUTES
                    and self._phase is ImportScenePhase.CREATE_POINT_ATTRIBUTES
                ):
                    break
                if (
                    phase_before is ImportScenePhase.CREATE_CAMERAS
                    and not self.done
                ):
                    break
        except Exception as exc:
            self._error = exc
            self._phase = ImportScenePhase.FAILED
            raise
        return self.progress

    def abort(self) -> None:
        """현재 Job이 만든 staging만 제거하고 보유 버퍼를 해제합니다."""

        if self._staged is not None:
            cleanup_staging(self._staged)
            self._staged = None
        self._point_ids = ()
        self._image_records = ()
        self._coordinates.clear()
        del self._colors[:]
        del self._errors[:]
        del self._point3d_id_low[:]
        del self._point3d_id_high[:]

    def release_staging_ownership(self) -> None:
        """성공 commit 뒤 Scene 소유가 된 staging을 Job 정리 대상에서 제외합니다."""

        if not self.done:
            raise RuntimeError("완료되지 않은 Scene Job의 staging 소유권을 해제할 수 없습니다.")
        self._staged = None
