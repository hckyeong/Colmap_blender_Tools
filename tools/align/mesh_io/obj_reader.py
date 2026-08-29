"""원본 OBJ를 줄 단위로 읽어 삼각형 정점·face-corner UV 배열로 확정합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Callable, TextIO

import numpy as np

from ..constants import OBJ_MAX_TEXTURE_COORDINATES, OBJ_MAX_TRIANGLE_FACES
from .types import MeshImportProgress
from .obj_materials import ObjMaterialError, resolve_obj_materials, split_obj_arguments
from .validation import MeshValidationError, validate_obj_limits, validate_triangle_mesh


class ObjReaderError(MeshValidationError):
    """OBJ 문법·파일 읽기·삼각면 제한 오류를 나타냅니다."""


@dataclass(frozen=True, slots=True)
class ObjParseResult:
    """OBJ 원본 형상·UV 배열과 표시용 파생 인덱스를 보관합니다."""

    vertices_float64: np.ndarray
    triangles_uint32: np.ndarray
    texcoords_float64: np.ndarray
    texcoord_component_counts_uint8: np.ndarray
    triangle_texcoord_indices_int32: np.ndarray
    materials: tuple[object, ...]
    triangle_material_indices_int32: np.ndarray
    registration_vertex_indices: np.ndarray
    bounds_min: np.ndarray
    bounds_max: np.ndarray

    @property
    def vertex_count(self) -> int:
        """파싱된 원본 정점 개수를 반환합니다."""
        return int(self.vertices_float64.shape[0])

    @property
    def triangle_count(self) -> int:
        """파싱된 유효 삼각면 개수를 반환합니다."""
        return int(self.triangles_uint32.shape[0])

    @property
    def texture_coordinate_count(self) -> int:
        """파싱된 OBJ UV 좌표 개수를 반환합니다."""
        return int(self.texcoords_float64.shape[0])

    @property
    def textured_face_count(self) -> int:
        """세 꼭짓점에 UV가 연결된 삼각면 개수를 반환합니다."""
        return int(np.count_nonzero(np.all(self.triangle_texcoord_indices_int32 >= 0, axis=1)))

    @property
    def material_count(self) -> int:
        """파싱된 OBJ 면에서 참조하는 재질 개수를 반환합니다."""

        return len(self.materials)

    @property
    def diffuse_texture_count(self) -> int:
        """현재 파일로 존재하는 고유 diffuse 텍스처 개수를 반환합니다."""

        return len(
            {
                material.diffuse_texture_path
                for material in self.materials
                if material.has_available_diffuse_texture
            }
        )


@dataclass(slots=True)
class _ObjMaterialParseState:
    """OBJ 줄 파싱 중 mtllib와 현재 usemtl의 면별 적용 상태를 누적합니다."""

    library_references: list[str]
    face_material_names: list[str | None]
    current_material_name: str | None = None


def _parse_float(token: str, label: str) -> float:
    """OBJ 좌표 토큰을 유한한 float64 값으로 변환합니다."""
    try:
        value = float(token)
    except (TypeError, ValueError) as error:
        raise ObjReaderError(f"OBJ {label} 좌표가 올바르지 않습니다.") from error
    if not np.isfinite(value):
        raise ObjReaderError(f"OBJ {label} 좌표에 NaN 또는 Infinity가 있습니다.")
    return value


def _resolve_vertex_index(token: str, vertex_count: int) -> int:
    """OBJ 양수·음수 정점 참조를 0 기반 인덱스로 변환합니다."""
    raw = str(token).strip()
    if not raw:
        raise ObjReaderError("OBJ face 정점 인덱스가 비어 있습니다.")
    try:
        index = int(raw)
    except ValueError as error:
        raise ObjReaderError(f"OBJ face 정점 인덱스가 정수가 아닙니다: {raw}") from error
    if index == 0:
        raise ObjReaderError("OBJ 정점 인덱스 0은 허용하지 않습니다.")
    resolved = index - 1 if index > 0 else vertex_count + index
    if resolved < 0 or resolved >= vertex_count:
        raise ObjReaderError(f"OBJ 정점 인덱스가 범위를 벗어났습니다: {index}")
    return resolved


def _face_index(token: str, vertex_count: int) -> int:
    """호환 호출부를 위해 OBJ face token에서 정점 인덱스만 반환합니다."""
    return _resolve_vertex_index(str(token).split("/", 1)[0], vertex_count)


def _resolve_texture_coordinate_index(token: str, texcoord_count: int) -> int:
    """OBJ 양수·음수 UV 참조를 0 기반 인덱스로 변환합니다."""
    raw = str(token).strip()
    if not raw:
        raise ObjReaderError("OBJ face UV 인덱스가 비어 있습니다.")
    try:
        index = int(raw)
    except ValueError as error:
        raise ObjReaderError(f"OBJ face UV 인덱스가 정수가 아닙니다: {raw}") from error
    if index == 0:
        raise ObjReaderError("OBJ UV 인덱스 0은 허용하지 않습니다.")
    resolved = index - 1 if index > 0 else texcoord_count + index
    if resolved < 0 or resolved >= texcoord_count:
        raise ObjReaderError(f"OBJ UV 인덱스가 범위를 벗어났습니다: {index}")
    return resolved


def _face_corner_indices(token: str, vertex_count: int, texcoord_count: int) -> tuple[int, int]:
    """OBJ face 꼭짓점 토큰에서 정점과 선택적 UV 인덱스를 읽습니다."""
    parts = str(token).strip().split("/")
    if not 1 <= len(parts) <= 3:
        raise ObjReaderError(f"OBJ face token 형식이 올바르지 않습니다: {token}")
    vertex_index = _resolve_vertex_index(parts[0], vertex_count)
    if len(parts) == 1:
        return vertex_index, -1
    if len(parts) == 2:
        if not parts[1]:
            raise ObjReaderError(f"OBJ face token 형식이 올바르지 않습니다: {token}")
        return vertex_index, _resolve_texture_coordinate_index(parts[1], texcoord_count)
    if not parts[2]:
        raise ObjReaderError(f"OBJ face normal 인덱스가 비어 있습니다: {token}")
    try:
        normal_index = int(parts[2])
    except ValueError as error:
        raise ObjReaderError(f"OBJ face normal 인덱스가 정수가 아닙니다: {parts[2]}") from error
    if normal_index == 0:
        raise ObjReaderError("OBJ normal 인덱스 0은 허용하지 않습니다.")
    texcoord_index = (
        -1
        if not parts[1]
        else _resolve_texture_coordinate_index(parts[1], texcoord_count)
    )
    return vertex_index, texcoord_index


def _parse_line(
    raw_line: str,
    line_count: int,
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
    texcoords: list[tuple[float, float, float]],
    texcoord_component_counts: list[int],
    triangle_texcoords: list[tuple[int, int, int]],
    material_state: _ObjMaterialParseState,
    max_faces: int,
) -> None:
    """OBJ 한 줄에서 원본 정점·UV 또는 삼각면 하나를 누적합니다."""
    stripped = raw_line.split("#", 1)[0].strip()
    if not stripped:
        return
    fields = stripped.split()
    record = fields[0].casefold()
    if record == "v":
        if len(fields) < 4:
            raise ObjReaderError(f"OBJ 정점 레코드가 올바르지 않습니다 (line {line_count}).")
        if len(vertices) >= np.iinfo(np.uint32).max:
            raise ObjReaderError("OBJ 정점 수가 uint32 인덱스 범위를 초과했습니다.")
        vertices.append(tuple(_parse_float(token, "정점") for token in fields[1:4]))
        return
    if record == "vt":
        component_count = len(fields) - 1
        if component_count < 1 or component_count > 3:
            raise ObjReaderError(f"OBJ UV 레코드는 1~3개 성분이어야 합니다 (line {line_count}).")
        if len(texcoords) >= OBJ_MAX_TEXTURE_COORDINATES:
            raise ObjReaderError(
                f"OBJ UV 좌표 수가 {OBJ_MAX_TEXTURE_COORDINATES:,}개 제한을 초과했습니다."
            )
        values = [_parse_float(token, "UV") for token in fields[1:]]
        values.extend((0.0,) * (3 - component_count))
        texcoords.append((values[0], values[1], values[2]))
        texcoord_component_counts.append(component_count)
        return
    if record == "mtllib":
        arguments = stripped[len(fields[0]) :].strip()
        references = split_obj_arguments(arguments)
        if not references:
            raise ObjReaderError(f"OBJ mtllib 경로가 비어 있습니다 (line {line_count}).")
        material_state.library_references.extend(references)
        return
    if record == "usemtl":
        arguments = stripped[len(fields[0]) :].strip()
        values = split_obj_arguments(arguments)
        if not values:
            raise ObjReaderError(f"OBJ usemtl 이름이 비어 있습니다 (line {line_count}).")
        material_name = " ".join(values).strip()
        material_state.current_material_name = (
            None if material_name.casefold() == "off" else material_name
        )
        return
    if record != "f":
        # vn, o, g, s 등은 현재 형상·UV·diffuse 재질 계약에 영향을 주지 않습니다.
        return
    if len(fields) != 4:
        raise ObjReaderError(
            f"OBJ face는 삼각형 정점 세 개만 지원합니다 (line {line_count})."
        )
    if len(triangles) >= max_faces:
        raise ObjReaderError("OBJ 삼각면 수가 600,000개 제한을 초과했습니다.")
    corners = tuple(
        _face_corner_indices(token, len(vertices), len(texcoords))
        for token in fields[1:4]
    )
    indices = tuple(corner[0] for corner in corners)
    uv_indices = tuple(corner[1] for corner in corners)
    if len(set(indices)) != 3:
        raise ObjReaderError(f"OBJ face가 같은 정점 인덱스를 반복합니다 (line {line_count}).")
    if any(index >= 0 for index in uv_indices) and not all(index >= 0 for index in uv_indices):
        raise ObjReaderError(
            f"OBJ 한 face의 세 꼭짓점은 모두 UV가 있거나 모두 없어야 합니다 (line {line_count})."
        )
    triangles.append(indices)
    triangle_texcoords.append(uv_indices)
    material_state.face_material_names.append(material_state.current_material_name)


def _finalize(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
    texcoords: list[tuple[float, float, float]],
    texcoord_component_counts: list[int],
    triangle_texcoords: list[tuple[int, int, int]],
    source_path: Path,
    material_state: _ObjMaterialParseState,
) -> ObjParseResult:
    """누적 형상·UV·재질을 불변 배열로 확정하고 등록 정점을 계산합니다."""
    vertex_array, triangle_array = validate_triangle_mesh(vertices, triangles)
    validate_obj_limits(vertex_count=vertex_array.shape[0], triangle_count=triangle_array.shape[0])
    registration = np.unique(triangle_array.reshape(-1)).astype(np.uint32, copy=False)
    registration = np.ascontiguousarray(registration, dtype=np.uint32)
    registration.setflags(write=False)
    bounds_min = np.ascontiguousarray(vertex_array.min(axis=0), dtype=np.float64)
    bounds_max = np.ascontiguousarray(vertex_array.max(axis=0), dtype=np.float64)
    bounds_min.setflags(write=False)
    bounds_max.setflags(write=False)
    texcoord_array = np.ascontiguousarray(np.asarray(texcoords, dtype=np.float64).reshape((-1, 3)))
    component_count_array = np.ascontiguousarray(
        np.asarray(texcoord_component_counts, dtype=np.uint8).reshape((-1,))
    )
    triangle_texcoord_array = np.ascontiguousarray(
        np.asarray(triangle_texcoords, dtype=np.int32).reshape((-1, 3))
    )
    texcoord_array.setflags(write=False)
    component_count_array.setflags(write=False)
    triangle_texcoord_array.setflags(write=False)
    materials, triangle_material_indices = resolve_obj_materials(
        source_path,
        material_state.library_references,
        material_state.face_material_names,
    )
    return ObjParseResult(
        vertices_float64=vertex_array,
        triangles_uint32=triangle_array,
        texcoords_float64=texcoord_array,
        texcoord_component_counts_uint8=component_count_array,
        triangle_texcoord_indices_int32=triangle_texcoord_array,
        materials=materials,
        triangle_material_indices_int32=triangle_material_indices,
        registration_vertex_indices=registration,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
    )


def _iter_lines(path: Path) -> TextIO:
    """OBJ 파일을 전체 read 없이 UTF-8 줄 iterator로 엽니다."""
    try:
        return path.open("r", encoding="utf-8-sig", errors="strict")
    except OSError as error:
        raise ObjReaderError(f"OBJ 파일을 열 수 없습니다: {path}") from error


def read_obj(
    path: str | Path,
    *,
    max_faces: int = OBJ_MAX_TRIANGLE_FACES,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[MeshImportProgress], None] | None = None,
) -> ObjParseResult:
    """삼각형 OBJ를 줄 단위로 읽고 600,000면 이내 형상·UV 배열을 반환합니다."""
    source_path = Path(path).expanduser()
    if source_path.suffix.casefold() != ".obj":
        raise ObjReaderError("OBJ 파일 확장자가 아닙니다.")
    if int(max_faces) != OBJ_MAX_TRIANGLE_FACES:
        # 1차 정책은 호출자가 임의로 완화하지 못하도록 고정합니다.
        max_faces = OBJ_MAX_TRIANGLE_FACES
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    texcoords: list[tuple[float, float, float]] = []
    texcoord_component_counts: list[int] = []
    triangle_texcoords: list[tuple[int, int, int]] = []
    material_state = _ObjMaterialParseState([], [])
    line_count = 0
    stream = _iter_lines(source_path)
    try:
        for raw_line in stream:
            line_count += 1
            if cancel_check is not None and cancel_check():
                raise ObjReaderError("OBJ 가져오기가 취소되었습니다.")
            _parse_line(
                raw_line,
                line_count,
                vertices,
                triangles,
                texcoords,
                texcoord_component_counts,
                triangle_texcoords,
                material_state,
                int(max_faces),
            )
            if progress_callback is not None and (line_count == 1 or line_count % 2048 == 0):
                progress_callback(
                    MeshImportProgress(
                        stage="READ_OBJ",
                        processed_lines=line_count,
                        vertex_count=len(vertices),
                        texture_coordinate_count=len(texcoords),
                        triangle_count=len(triangles),
                        message="OBJ 형상과 UV를 읽는 중",
                    )
                )
    except UnicodeError as error:
        raise ObjReaderError(f"OBJ 파일 인코딩을 읽을 수 없습니다: {source_path}") from error
    finally:
        stream.close()
    try:
        result = _finalize(
            vertices,
            triangles,
            texcoords,
            texcoord_component_counts,
            triangle_texcoords,
            source_path,
            material_state,
        )
    except (MeshValidationError, ObjMaterialError) as error:
        raise ObjReaderError(str(error)) from error
    if progress_callback is not None:
        progress_callback(
            MeshImportProgress(
                stage="COMPLETED",
                processed_lines=line_count,
                vertex_count=result.vertex_count,
                texture_coordinate_count=result.texture_coordinate_count,
                triangle_count=result.triangle_count,
                fraction=1.0,
                message="OBJ 형상과 UV 읽기 완료",
            )
        )
    return result


parse_obj = read_obj


class ObjReader:
    """TIMER 호출부가 OBJ 형상·UV read를 진행 상태와 함께 구동하도록 감싼 reader입니다."""

    def __init__(self, path: str | Path, *, max_faces: int = OBJ_MAX_TRIANGLE_FACES) -> None:
        """OBJ 경로와 하드 면·UV 제한을 저장하고 아직 읽지 않은 상태로 초기화합니다."""
        self.path = Path(path).expanduser()
        self.max_faces = OBJ_MAX_TRIANGLE_FACES if int(max_faces) != OBJ_MAX_TRIANGLE_FACES else int(max_faces)
        self.progress = MeshImportProgress(stage="FILE_CHECK")
        self.result: ObjParseResult | None = None
        self.error: Exception | None = None
        self.done = False
        self.cancelled = False
        self._stream: TextIO | None = None
        self._vertices: list[tuple[float, float, float]] = []
        self._triangles: list[tuple[int, int, int]] = []
        self._texcoords: list[tuple[float, float, float]] = []
        self._texcoord_component_counts: list[int] = []
        self._triangle_texcoords: list[tuple[int, int, int]] = []
        self._material_state = _ObjMaterialParseState([], [])
        self._line_count = 0
        try:
            self._total_bytes = max(1, int(self.path.stat().st_size))
        except OSError:
            self._total_bytes = 1

    def _close_stream(self) -> None:
        """열린 OBJ 텍스트 스트림을 한 번만 닫습니다."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()

    def _open_stream(self) -> TextIO:
        """첫 증분 단계에서 OBJ 텍스트 스트림을 열어 재사용합니다."""
        if self._stream is None:
            self._stream = _iter_lines(self.path)
        return self._stream

    def _fraction(self) -> float:
        """현재 텍스트 스트림 위치를 파일 바이트 진행률로 변환합니다."""
        stream = self._stream
        if stream is None:
            return 1.0 if self.done else 0.0
        try:
            completed_bytes = max(0, int(stream.tell()))
        except (OSError, ValueError):
            return 0.0
        return min(1.0, completed_bytes / self._total_bytes)

    def _complete(self) -> None:
        """누적 목록을 불변 배열로 확정하고 스트림·임시 목록을 해제합니다."""
        self.result = _finalize(
            self._vertices,
            self._triangles,
            self._texcoords,
            self._texcoord_component_counts,
            self._triangle_texcoords,
            self.path,
            self._material_state,
        )
        self._vertices = []
        self._triangles = []
        self._texcoords = []
        self._texcoord_component_counts = []
        self._triangle_texcoords = []
        self._material_state = _ObjMaterialParseState([], [])
        self.done = True
        self._close_stream()
        self.progress = MeshImportProgress(
            stage="COMPLETED",
            processed_lines=self._line_count,
            vertex_count=self.result.vertex_count,
            texture_coordinate_count=self.result.texture_coordinate_count,
            triangle_count=self.result.triangle_count,
            fraction=1.0,
            message="OBJ 형상과 UV 읽기 완료",
        )

    def step(self, time_budget_seconds: float = 0.025) -> MeshImportProgress:
        """한 TIMER tick에서 OBJ 읽기를 실행하고 완료·실패 상태를 갱신합니다."""
        if self.done or self.cancelled:
            return self.progress
        try:
            budget = float(time_budget_seconds)
            if not np.isfinite(budget) or budget < 0.0:
                raise ObjReaderError("OBJ 읽기 시간 예산이 올바르지 않습니다.")
            stream = self._open_stream()
            started_at = perf_counter()
            processed_this_step = 0
            while True:
                raw_line = stream.readline()
                if raw_line == "":
                    self._complete()
                    break
                self._line_count += 1
                processed_this_step += 1
                _parse_line(
                    raw_line,
                    self._line_count,
                    self._vertices,
                    self._triangles,
                    self._texcoords,
                    self._texcoord_component_counts,
                    self._triangle_texcoords,
                    self._material_state,
                    self.max_faces,
                )
                if processed_this_step % 256 == 0 and perf_counter() - started_at >= budget:
                    self.progress = MeshImportProgress(
                        stage="READ_OBJ",
                        processed_lines=self._line_count,
                        vertex_count=len(self._vertices),
                        texture_coordinate_count=len(self._texcoords),
                        triangle_count=len(self._triangles),
                        fraction=self._fraction(),
                        message="OBJ 형상과 UV를 읽는 중",
                    )
                    break
        except UnicodeError as error:
            self.error = ObjReaderError(f"OBJ 파일 인코딩을 읽을 수 없습니다: {self.path}")
            self.done = True
            self._close_stream()
            self.progress = MeshImportProgress(
                stage="FAILED",
                processed_lines=self._line_count,
                vertex_count=len(self._vertices),
                texture_coordinate_count=len(self._texcoords),
                triangle_count=len(self._triangles),
                message=str(self.error),
            )
        except Exception as error:
            self.error = error
            self.done = True
            self._close_stream()
            self.progress = MeshImportProgress(
                stage="FAILED",
                processed_lines=self._line_count,
                vertex_count=len(self._vertices),
                texture_coordinate_count=len(self._texcoords),
                triangle_count=len(self._triangles),
                message=str(error),
            )
        return self.progress

    def cancel(self) -> None:
        """아직 완료되지 않은 reader를 취소 상태로 전환합니다."""
        if not self.done:
            self.cancelled = True
            self._close_stream()
            self._vertices = []
            self._triangles = []
            self._texcoords = []
            self._texcoord_component_counts = []
            self._triangle_texcoords = []
            self._material_state = _ObjMaterialParseState([], [])
            self.progress = MeshImportProgress(stage="CANCELLED", message="OBJ 가져오기가 취소되었습니다.")

    @property
    def model(self) -> ObjParseResult:
        """완료된 OBJ 결과를 반환하고 미완료 접근을 거부합니다."""
        if not self.done or self.result is None:
            if self.error is not None:
                raise self.error
            raise ObjReaderError("OBJ reader가 완료되지 않았습니다.")
        return self.result


__all__ = ("ObjParseResult", "ObjReader", "ObjReaderError", "parse_obj", "read_obj")
