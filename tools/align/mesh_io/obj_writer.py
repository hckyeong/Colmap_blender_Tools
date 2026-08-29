"""정렬된 삼각형 메시를 canonical OBJ로 증분 기록하고 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable

import numpy as np

from .obj_reader import ObjReaderError, read_obj
from .types import ObjMaterialData


_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_VERTEX_CHUNK_SIZE = 4096
_DEFAULT_TEXCOORD_CHUNK_SIZE = 4096
_DEFAULT_FACE_CHUNK_SIZE = 4096
_DEFAULT_TEXTURE_CHUNK_SIZE = 1024 * 1024
_WINDOWS_MAX_VISIBLE_PATH_LENGTH = 259


def _matrix_value(value: Any, label: str) -> np.ndarray:
    """행렬 객체 또는 배열을 유한한 4×4 float64 배열로 변환합니다."""

    matrix = getattr(value, "matrix_float64", value)
    try:
        result = np.ascontiguousarray(matrix, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}은 4×4 float64 행렬이어야 합니다.") from error
    if result.shape != (4, 4) or not np.isfinite(result).all():
        raise ValueError(f"{label}은 유한한 4×4 float64 행렬이어야 합니다.")
    return result


def compose_project_world_matrix(alignment: Any, display_axis: Any) -> np.ndarray:
    """확정 Alignment와 Display Axis를 Project World column-vector 순서로 합성합니다."""

    alignment_matrix = _matrix_value(alignment, "Alignment")
    display_matrix = _matrix_value(display_axis, "Display Axis")
    matrix = np.ascontiguousarray(alignment_matrix @ display_matrix, dtype=np.float64)
    if not np.isfinite(matrix).all():
        raise ValueError("Project World 합성 행렬에 유한하지 않은 값이 있습니다.")
    determinant = float(np.linalg.det(matrix[:3, :3]))
    if not np.isfinite(determinant) or abs(determinant) <= np.finfo(np.float64).eps:
        raise ValueError("Project World 합성 행렬의 선형부가 가역이 아닙니다.")
    matrix.setflags(write=False)
    return matrix


def sanitize_obj_name(display_name: str) -> str:
    """레이어 이름에서 Windows 금지 문자·제어 문자·끝 공백을 제거합니다."""

    normalized = _WINDOWS_FORBIDDEN.sub("_", str(display_name))
    normalized = _CONTROL_CHARACTER.sub("_", normalized)
    normalized = normalized.strip()
    normalized = normalized.rstrip(" .")
    if not normalized:
        normalized = "mesh"
    # Windows 예약 장치명과의 충돌도 출력 폴더 계약 안에서 피합니다.
    if normalized.casefold().split(".", 1)[0] in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }:
        normalized = f"_{normalized}"
    return normalized


def model_id_short_hash(model_id: str) -> str:
    """모델 ID에서 반복 실행에도 같은 짧은 BLAKE2 파일명 지문을 생성합니다."""

    return hashlib.blake2b(
        str(model_id).encode("utf-8"),
        digest_size=8,
        person=b"colmap-obj",
    ).hexdigest()


def canonical_obj_filename(sequence: int, display_name: str, model_id: str) -> str:
    """모델 표시 이름만 안전하게 정규화해 canonical OBJ 파일명으로 만듭니다."""

    normalized_sequence = int(sequence)
    if normalized_sequence < 1:
        raise ValueError("OBJ 내보내기 순번은 1 이상이어야 합니다.")
    # 기존 공개 호출 형식을 유지하되 순번과 모델 ID는 파일명에 포함하지 않습니다.
    return f"{sanitize_obj_name(display_name)}.obj"


def validate_windows_output_path(path: str | Path, label: str) -> Path:
    """Windows 260자 경계를 넘는 출력 경로를 파일 작업 전에 명확한 원인으로 거부합니다."""

    target = Path(path).expanduser().absolute()
    path_length = len(str(target))
    if os.name == "nt" and path_length > _WINDOWS_MAX_VISIBLE_PATH_LENGTH:
        remedy = (
            "더 짧은 출력 폴더를 선택하거나 원본 텍스처 파일명을 줄이십시오."
            if "텍스처" in str(label)
            else "더 짧은 출력 폴더를 선택하거나 모델 표시 이름을 줄이십시오."
        )
        raise ValueError(
            f"{label}가 Windows 경로 길이 제한 260자를 초과했습니다 "
            f"(현재 {path_length}자). {remedy} 경로: {target}"
        )
    return target


def _freeze_snapshot_array(value: Any, shape: tuple[int | None, ...], label: str, dtype: Any) -> np.ndarray:
    """snapshot 배열의 형상·dtype·유한성을 검증하고 writer가 읽을 연속 배열을 반환합니다."""

    try:
        array = np.asarray(value, dtype=dtype)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 배열을 읽을 수 없습니다.") from error
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise ValueError(f"{label} 배열 형상이 올바르지 않습니다.")
    if not np.isfinite(array).all():
        raise ValueError(f"{label} 배열에 유한하지 않은 값이 있습니다.")
    return np.ascontiguousarray(array, dtype=dtype)


@dataclass(frozen=True, slots=True)
class ObjTextureAssetSnapshot:
    """내보낼 원본 텍스처와 후보 폴더 안의 충돌 없는 상대 경로를 고정합니다."""

    source_path: Path
    output_relative_path: Path
    source_size: int
    source_mtime_ns: int

    def __post_init__(self) -> None:
        """원본 일반 파일과 안전한 상대 출력 경로·파일 상태를 검증합니다."""

        source = Path(self.source_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"OBJ 텍스처 원본이 일반 파일이 아닙니다: {source}")
        relative = Path(self.output_relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("OBJ 텍스처 출력 경로는 안전한 상대 경로여야 합니다.")
        stat = source.stat()
        if int(self.source_size) != int(stat.st_size) or int(self.source_mtime_ns) != int(stat.st_mtime_ns):
            raise ValueError(f"OBJ 텍스처 원본이 snapshot 생성 중 변경되었습니다: {source}")
        object.__setattr__(self, "source_path", source)
        object.__setattr__(self, "output_relative_path", relative)
        object.__setattr__(self, "source_size", int(stat.st_size))
        object.__setattr__(self, "source_mtime_ns", int(stat.st_mtime_ns))


def _texture_asset_snapshots(
    output_filename: str,
    materials: tuple[ObjMaterialData, ...],
) -> tuple[ObjTextureAssetSnapshot, ...]:
    """존재하는 고유 diffuse 원본마다 Export 루트의 원본 파일명 snapshot을 만듭니다."""

    texture_paths: list[Path] = []
    for material in materials:
        if not material.has_available_diffuse_texture or material.diffuse_texture_path is None:
            continue
        source = material.diffuse_texture_path.resolve(strict=True)
        if source not in texture_paths:
            texture_paths.append(source)
    assets = []
    for source in texture_paths:
        stat = source.stat()
        safe_name = sanitize_obj_name(source.name)
        assets.append(
            ObjTextureAssetSnapshot(
                source_path=source,
                output_relative_path=Path(safe_name),
                source_size=int(stat.st_size),
                source_mtime_ns=int(stat.st_mtime_ns),
            )
        )
    return tuple(assets)


@dataclass(frozen=True, slots=True)
class ObjExportModelSnapshot:
    """한 A 그룹 메시의 불변 OBJ·재질·텍스처 기록 입력과 stale 지문을 보관합니다."""

    model_id: str
    display_name: str
    source_path: Path
    output_filename: str
    load_order: int
    geometry_revision: int
    vertex_count: int
    face_count: int
    source_fingerprint: str
    display_fingerprint: str
    alignment_fingerprint: str
    project_world_matrix_float64: np.ndarray
    vertices_float64: np.ndarray
    triangles_uint32: np.ndarray
    texcoords_float64: np.ndarray = field(
        default_factory=lambda: np.empty((0, 3), dtype=np.float64)
    )
    texcoord_component_counts_uint8: np.ndarray = field(
        default_factory=lambda: np.empty((0,), dtype=np.uint8)
    )
    triangle_texcoord_indices_int32: np.ndarray = field(
        default_factory=lambda: np.empty((0, 3), dtype=np.int32)
    )
    materials: tuple[ObjMaterialData, ...] = ()
    triangle_material_indices_int32: np.ndarray = field(
        default_factory=lambda: np.empty((0,), dtype=np.int32)
    )
    texture_assets: tuple[ObjTextureAssetSnapshot, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        """snapshot 식별자·배열·Project World 행렬을 기록 전에 엄격히 검증합니다."""

        model_id = str(self.model_id).strip()
        display_name = str(self.display_name).strip()
        filename = Path(str(self.output_filename)).name
        if not model_id or not display_name or not filename or filename != str(self.output_filename):
            raise ValueError("OBJ snapshot 식별자·이름·파일명이 올바르지 않습니다.")
        vertices = _freeze_snapshot_array(self.vertices_float64, (None, 3), "OBJ 정점", np.float64)
        triangles = _freeze_snapshot_array(self.triangles_uint32, (None, 3), "OBJ 삼각면", np.uint32)
        if vertices.shape[0] < 1 or triangles.shape[0] < 1:
            raise ValueError("OBJ snapshot에는 정점과 삼각면이 필요합니다.")
        if np.any(triangles >= vertices.shape[0]):
            raise ValueError("OBJ snapshot face 인덱스가 범위를 벗어났습니다.")
        if np.any(triangles[:, 0] == triangles[:, 1]) or np.any(triangles[:, 1] == triangles[:, 2]) or np.any(triangles[:, 0] == triangles[:, 2]):
            raise ValueError("OBJ snapshot face에 반복 정점 인덱스가 있습니다.")
        texcoords = _freeze_snapshot_array(
            self.texcoords_float64,
            (None, 3),
            "OBJ UV 좌표",
            np.float64,
        )
        component_counts = _freeze_snapshot_array(
            self.texcoord_component_counts_uint8,
            (None,),
            "OBJ UV 성분 수",
            np.uint8,
        )
        if component_counts.shape[0] != texcoords.shape[0]:
            raise ValueError("OBJ snapshot UV 좌표와 성분 수가 일치하지 않습니다.")
        if component_counts.size and (
            np.any(component_counts < 1) or np.any(component_counts > 3)
        ):
            raise ValueError("OBJ snapshot UV 성분 수는 1~3이어야 합니다.")
        raw_face_texcoords = np.asarray(self.triangle_texcoord_indices_int32, dtype=np.int32)
        if raw_face_texcoords.shape == (0, 3) and texcoords.shape[0] == 0:
            raw_face_texcoords = np.full(triangles.shape, -1, dtype=np.int32)
        face_texcoords = _freeze_snapshot_array(
            raw_face_texcoords,
            (triangles.shape[0], 3),
            "OBJ 면 UV 인덱스",
            np.int32,
        )
        textured_rows = np.all(face_texcoords >= 0, axis=1)
        untextured_rows = np.all(face_texcoords == -1, axis=1)
        if not np.all(textured_rows | untextured_rows):
            raise ValueError("OBJ snapshot 한 면은 UV가 모두 있거나 모두 없어야 합니다.")
        if np.any(face_texcoords[textured_rows] >= texcoords.shape[0]):
            raise ValueError("OBJ snapshot 면 UV 인덱스가 범위를 벗어났습니다.")
        materials = tuple(self.materials)
        if any(not isinstance(material, ObjMaterialData) for material in materials):
            raise ValueError("OBJ snapshot 재질 배열에 지원하지 않는 값이 있습니다.")
        if len({material.name for material in materials}) != len(materials):
            raise ValueError("OBJ snapshot 재질 이름이 중복되었습니다.")
        raw_face_materials = np.asarray(self.triangle_material_indices_int32, dtype=np.int32)
        if raw_face_materials.shape == (0,) and not materials:
            raw_face_materials = np.full((triangles.shape[0],), -1, dtype=np.int32)
        face_materials = _freeze_snapshot_array(
            raw_face_materials,
            (triangles.shape[0],),
            "OBJ 면 재질 인덱스",
            np.int32,
        )
        if np.any(face_materials < -1) or np.any(face_materials >= len(materials)):
            raise ValueError("OBJ snapshot 면 재질 인덱스가 재질 배열 범위를 벗어났습니다.")
        matrix = _matrix_value(self.project_world_matrix_float64, "Project World")
        vertex_count = int(self.vertex_count)
        face_count = int(self.face_count)
        if vertex_count != vertices.shape[0] or face_count != triangles.shape[0]:
            raise ValueError("OBJ snapshot count가 원본 배열과 일치하지 않습니다.")
        revision = int(self.geometry_revision)
        if revision < 0:
            raise ValueError("OBJ snapshot geometry revision은 음수일 수 없습니다.")
        load_order = int(self.load_order)
        if load_order < 0:
            raise ValueError("OBJ snapshot load order는 음수일 수 없습니다.")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "source_path", Path(self.source_path).expanduser())
        object.__setattr__(self, "output_filename", filename)
        object.__setattr__(self, "load_order", load_order)
        object.__setattr__(self, "geometry_revision", revision)
        object.__setattr__(self, "vertex_count", vertex_count)
        object.__setattr__(self, "face_count", face_count)
        object.__setattr__(self, "source_fingerprint", str(self.source_fingerprint).strip().lower())
        object.__setattr__(self, "display_fingerprint", str(self.display_fingerprint).strip())
        object.__setattr__(self, "alignment_fingerprint", str(self.alignment_fingerprint).strip())
        matrix.setflags(write=False)
        vertices.setflags(write=False)
        triangles.setflags(write=False)
        texcoords.setflags(write=False)
        component_counts.setflags(write=False)
        face_texcoords.setflags(write=False)
        face_materials.setflags(write=False)
        object.__setattr__(self, "project_world_matrix_float64", matrix)
        object.__setattr__(self, "vertices_float64", vertices)
        object.__setattr__(self, "triangles_uint32", triangles)
        object.__setattr__(self, "texcoords_float64", texcoords)
        object.__setattr__(self, "texcoord_component_counts_uint8", component_counts)
        object.__setattr__(self, "triangle_texcoord_indices_int32", face_texcoords)
        object.__setattr__(self, "materials", materials)
        object.__setattr__(self, "triangle_material_indices_int32", face_materials)
        texture_assets = _texture_asset_snapshots(filename, materials)
        texture_names = tuple(
            asset.output_relative_path.as_posix().casefold() for asset in texture_assets
        )
        if len(set(texture_names)) != len(texture_names):
            raise ValueError(
                "한 OBJ에서 서로 다른 원본 텍스처가 같은 파일명을 사용합니다. "
                "원본 이미지 파일명을 고유하게 변경하십시오."
            )
        object.__setattr__(self, "texture_assets", texture_assets)

    @property
    def has_reflection(self) -> bool:
        """Project World 선형부의 음의 행렬식을 읽어 face winding 반전 여부를 반환합니다."""

        return float(np.linalg.det(self.project_world_matrix_float64[:3, :3])) < 0.0

    @property
    def texture_coordinate_count(self) -> int:
        """snapshot에 보존된 OBJ UV 좌표 개수를 반환합니다."""

        return int(self.texcoords_float64.shape[0])

    @property
    def textured_face_count(self) -> int:
        """snapshot에서 UV가 세 꼭짓점에 모두 연결된 면 개수를 반환합니다."""

        return int(np.count_nonzero(np.all(self.triangle_texcoord_indices_int32 >= 0, axis=1)))

    @property
    def uv_coverage(self) -> float:
        """전체 삼각면 중 UV가 연결된 면의 비율을 반환합니다."""

        return self.textured_face_count / self.face_count

    @property
    def material_count(self) -> int:
        """snapshot에서 면이 참조할 수 있는 재질 개수를 반환합니다."""

        return len(self.materials)

    @property
    def diffuse_texture_count(self) -> int:
        """원본 그대로 복사할 고유 diffuse 텍스처 개수를 반환합니다."""

        return len(self.texture_assets)

    @property
    def material_library_filename(self) -> str:
        """OBJ와 같은 기본 이름을 쓰는 생성 MTL 파일명을 반환합니다."""

        return Path(self.output_filename).with_suffix(".mtl").name

    def texture_asset_for_material(self, material: ObjMaterialData) -> ObjTextureAssetSnapshot | None:
        """재질의 diffuse 원본에 대응하는 복사 asset을 반환합니다."""

        source = material.diffuse_texture_path
        if source is None:
            return None
        normalized = source.resolve(strict=False)
        return next((asset for asset in self.texture_assets if asset.source_path == normalized), None)


@dataclass(frozen=True, slots=True)
class ObjRoundTripValidation:
    """기록한 OBJ의 형상·UV·재질·복사 텍스처 왕복 결과를 보관합니다."""

    vertex_count: int
    face_count: int
    texture_coordinate_count: int
    textured_face_count: int
    uv_coverage: float
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    output_sha256: str
    material_count: int = 0
    diffuse_texture_count: int = 0
    copied_texture_sha256: tuple[tuple[str, str], ...] = ()


def transformed_vertices(snapshot: ObjExportModelSnapshot, start: int = 0, stop: int | None = None) -> np.ndarray:
    """snapshot 원본 정점 구간에 Project World 행렬을 적용해 새 float64 배열을 반환합니다."""

    begin = int(start)
    end = snapshot.vertex_count if stop is None else int(stop)
    if begin < 0 or end < begin or end > snapshot.vertex_count:
        raise ValueError("OBJ 정점 chunk 범위가 올바르지 않습니다.")
    source = snapshot.vertices_float64[begin:end]
    homogeneous = np.ones((source.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = source
    transformed_homogeneous = homogeneous @ snapshot.project_world_matrix_float64.T
    weights = transformed_homogeneous[:, 3]
    if np.any(~np.isfinite(weights)) or np.any(np.abs(weights) <= np.finfo(np.float64).eps):
        raise ValueError("Project World 변환의 동차 좌표가 유효하지 않습니다.")
    transformed = transformed_homogeneous[:, :3] / weights[:, np.newaxis]
    if not np.isfinite(transformed).all():
        raise ValueError("Project World 정점에 유한하지 않은 값이 있습니다.")
    return np.ascontiguousarray(transformed, dtype=np.float64)


def transformed_faces(snapshot: ObjExportModelSnapshot, start: int = 0, stop: int | None = None) -> np.ndarray:
    """원본 삼각면 구간을 1 기반 OBJ 인덱스로 바꾸고 reflection이면 winding을 반전합니다."""

    begin = int(start)
    end = snapshot.face_count if stop is None else int(stop)
    if begin < 0 or end < begin or end > snapshot.face_count:
        raise ValueError("OBJ face chunk 범위가 올바르지 않습니다.")
    faces = np.array(snapshot.triangles_uint32[begin:end], dtype=np.uint64, copy=True) + 1
    if snapshot.has_reflection:
        faces[:, [1, 2]] = faces[:, [2, 1]]
    return faces


def transformed_face_texcoord_indices(
    snapshot: ObjExportModelSnapshot,
    start: int = 0,
    stop: int | None = None,
) -> np.ndarray:
    """면 UV 인덱스 구간을 복사하고 reflection이면 정점과 같은 코너 순서로 반전합니다."""

    begin = int(start)
    end = snapshot.face_count if stop is None else int(stop)
    if begin < 0 or end < begin or end > snapshot.face_count:
        raise ValueError("OBJ 면 UV chunk 범위가 올바르지 않습니다.")
    indices = np.array(
        snapshot.triangle_texcoord_indices_int32[begin:end],
        dtype=np.int32,
        copy=True,
    )
    if snapshot.has_reflection:
        indices[:, [1, 2]] = indices[:, [2, 1]]
    return indices


def _format_coordinate(value: float) -> str:
    """OBJ 좌표를 locale과 무관한 결정적 17자리 유효 숫자로 직렬화합니다."""

    numeric = float(value)
    if numeric == 0.0:
        numeric = 0.0
    return format(numeric, ".17g")


def _quote_obj_argument(value: str) -> str:
    """공백이 있는 OBJ·MTL 경로를 파서가 다시 읽을 수 있는 따옴표 형식으로 만듭니다."""

    normalized = str(value).replace("\\", "/")
    if '"' in normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("OBJ/MTL 인수에 지원하지 않는 따옴표 또는 줄바꿈이 있습니다.")
    return f'"{normalized}"' if any(character.isspace() for character in normalized) else normalized


def material_library_text(snapshot: ObjExportModelSnapshot) -> str:
    """snapshot 재질과 복사 텍스처 상대 경로로 결정적 MTL 문서를 만듭니다."""

    lines = ["# COLMAP Align generated material library"]
    for material in snapshot.materials:
        lines.append("")
        lines.append(f"newmtl {material.name}")
        lines.append(
            "Kd " + " ".join(_format_coordinate(value) for value in material.diffuse_color)
        )
        asset = snapshot.texture_asset_for_material(material)
        if asset is not None:
            lines.append(f"map_Kd {_quote_obj_argument(asset.output_relative_path.as_posix())}")
    return "\n".join(lines) + "\n"


def write_material_library(path: str | Path, snapshot: ObjExportModelSnapshot) -> None:
    """재질이 있는 snapshot의 생성 MTL을 UTF-8로 기록하고 디스크에 반영합니다."""

    if snapshot.material_count == 0:
        return
    target = validate_windows_output_path(path, "MTL 출력 경로")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(material_library_text(snapshot), encoding="utf-8", newline="\n")
    with target.open("rb") as stream:
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass


def copy_texture_assets(
    output_root: str | Path,
    snapshot: ObjExportModelSnapshot,
    *,
    chunk_size: int = _DEFAULT_TEXTURE_CHUNK_SIZE,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[tuple[str, str], ...]:
    """원본 이미지 바이트를 그대로 복사하고 양쪽 SHA-256이 같은지 검증합니다."""

    destination_root = Path(output_root)
    copied: list[tuple[str, str]] = []
    chunk_bytes = max(1, int(chunk_size))
    for asset in snapshot.texture_assets:
        if cancel_check is not None and cancel_check():
            raise RuntimeError("OBJ 텍스처 내보내기가 취소되었습니다.")
        before = asset.source_path.stat()
        if int(before.st_size) != asset.source_size or int(before.st_mtime_ns) != asset.source_mtime_ns:
            raise RuntimeError(f"OBJ 텍스처 원본이 내보내기 중 변경되었습니다: {asset.source_path}")
        destination = destination_root / asset.output_relative_path
        validate_windows_output_path(destination, "OBJ 텍스처 출력 경로")
        if destination.resolve(strict=False) == asset.source_path:
            source_hash = sha256_file(asset.source_path)
            copied.append((asset.output_relative_path.as_posix(), source_hash))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with asset.source_path.open("rb") as source_stream, destination.open("xb") as target_stream:
            while True:
                if cancel_check is not None and cancel_check():
                    raise RuntimeError("OBJ 텍스처 내보내기가 취소되었습니다.")
                chunk = source_stream.read(chunk_bytes)
                if not chunk:
                    break
                target_stream.write(chunk)
                digest.update(chunk)
            target_stream.flush()
            try:
                os.fsync(target_stream.fileno())
            except OSError:
                pass
        after = asset.source_path.stat()
        if (
            int(after.st_size) != asset.source_size
            or int(after.st_mtime_ns) != asset.source_mtime_ns
            or destination.stat().st_size != asset.source_size
        ):
            raise RuntimeError(f"OBJ 텍스처 원본 또는 복사본이 내보내기 중 변경되었습니다: {asset.source_path}")
        output_digest = sha256_file(destination)
        if output_digest != digest.hexdigest():
            raise RuntimeError(f"OBJ 텍스처 복사본이 원본 바이트와 다릅니다: {destination}")
        copied.append((asset.output_relative_path.as_posix(), output_digest))
    return tuple(copied)


class IncrementalObjWriter:
    """한 OBJ 파일의 header·정점·UV·재질 face를 chunk 단위로 기록하는 파일 소유자입니다."""

    def __init__(self, path: str | Path, snapshot: ObjExportModelSnapshot) -> None:
        """출력 경로를 만들고 snapshot header를 기록할 준비를 합니다."""

        self.path = validate_windows_output_path(path, "OBJ 출력 경로")
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise ValueError("OBJ 출력 경로가 일반 파일이 아닙니다.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self._stream = self.path.open("w", encoding="utf-8", newline="\n")
        self._vertex_offset = 0
        self._texcoord_offset = 0
        self._face_offset = 0
        self._current_material_index: int | None = None
        self._closed = False
        self._stream.write("# COLMAP Align aligned OBJ\n")
        self._stream.write(f"# model_id: {snapshot.model_id}\n")
        if snapshot.material_count:
            self._stream.write(
                f"mtllib {_quote_obj_argument(snapshot.material_library_filename)}\n"
            )
        self._stream.write(f"o {sanitize_obj_name(snapshot.display_name)}\n")

    def write_vertices(self, start: int, stop: int | None = None) -> int:
        """정점 chunk를 Project World 좌표로 변환해 원본 순서대로 기록합니다."""

        self._ensure_open()
        begin = int(start)
        end = self.snapshot.vertex_count if stop is None else int(stop)
        if begin != self._vertex_offset:
            raise ValueError("OBJ 정점 chunk가 순서대로 전달되지 않았습니다.")
        values = transformed_vertices(self.snapshot, begin, end)
        for x, y, z in values:
            self._stream.write(
                f"v {_format_coordinate(x)} {_format_coordinate(y)} {_format_coordinate(z)}\n"
            )
        self._vertex_offset = end
        return end - begin

    def write_texture_coordinates(self, start: int, stop: int | None = None) -> int:
        """UV 좌표 chunk를 원본 성분 수와 순서대로 기록합니다."""

        self._ensure_open()
        begin = int(start)
        end = self.snapshot.texture_coordinate_count if stop is None else int(stop)
        if begin != self._texcoord_offset:
            raise ValueError("OBJ UV chunk가 순서대로 전달되지 않았습니다.")
        if begin < 0 or end < begin or end > self.snapshot.texture_coordinate_count:
            raise ValueError("OBJ UV chunk 범위가 올바르지 않습니다.")
        for values, component_count in zip(
            self.snapshot.texcoords_float64[begin:end],
            self.snapshot.texcoord_component_counts_uint8[begin:end],
            strict=True,
        ):
            components = " ".join(
                _format_coordinate(value) for value in values[: int(component_count)]
            )
            self._stream.write(f"vt {components}\n")
        self._texcoord_offset = end
        return end - begin

    def write_faces(self, start: int, stop: int | None = None) -> int:
        """삼각면 chunk를 원본 정점·UV·재질 연결과 1 기반 인덱스로 기록합니다."""

        self._ensure_open()
        begin = int(start)
        end = self.snapshot.face_count if stop is None else int(stop)
        if begin != self._face_offset:
            raise ValueError("OBJ face chunk가 순서대로 전달되지 않았습니다.")
        values = transformed_faces(self.snapshot, begin, end)
        uv_indices = transformed_face_texcoord_indices(self.snapshot, begin, end)
        material_indices = self.snapshot.triangle_material_indices_int32[begin:end]
        for (i, j, k), (ui, uj, uk), material_index in zip(
            values,
            uv_indices,
            material_indices,
            strict=True,
        ):
            normalized_material_index = int(material_index)
            if normalized_material_index != self._current_material_index:
                if normalized_material_index < 0:
                    self._stream.write("usemtl off\n")
                else:
                    self._stream.write(
                        f"usemtl {self.snapshot.materials[normalized_material_index].name}\n"
                    )
                self._current_material_index = normalized_material_index
            if int(ui) >= 0:
                self._stream.write(
                    f"f {int(i)}/{int(ui) + 1} {int(j)}/{int(uj) + 1} {int(k)}/{int(uk) + 1}\n"
                )
            else:
                self._stream.write(f"f {int(i)} {int(j)} {int(k)}\n")
        self._face_offset = end
        return end - begin

    def flush(self) -> None:
        """현재 chunk를 파일 스트림에 flush해 TIMER 작업의 진행 결과를 보존합니다."""

        self._ensure_open()
        self._stream.flush()
        try:
            os.fsync(self._stream.fileno())
        except OSError:
            # 파일 시스템이 fsync를 지원하지 않아도 flush 결과 자체는 유효합니다.
            pass

    def close(self) -> None:
        """OBJ 파일 handle을 flush·close하고 이후 기록을 금지합니다."""

        if self._closed:
            return
        try:
            self._stream.flush()
            self._stream.close()
        finally:
            self._closed = True

    def _ensure_open(self) -> None:
        """닫힌 writer에 대한 기록 요청을 즉시 거부합니다."""

        if self._closed:
            raise ValueError("OBJ writer가 이미 닫혔습니다.")

    def __enter__(self) -> "IncrementalObjWriter":
        """with 문에서 writer 자신을 반환합니다."""

        return self

    def __exit__(self, _exception_type: Any, _exception: Any, _traceback: Any) -> None:
        """with 문을 빠져나갈 때 파일 handle을 닫습니다."""

        self.close()


def sha256_file(path: str | Path) -> str:
    """출력 OBJ 또는 보고서 파일의 SHA-256을 chunk 단위로 계산합니다."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_obj_material_links(
    parsed: Any,
    snapshot: ObjExportModelSnapshot,
    output_root: str | Path,
) -> None:
    """왕복 파싱 재질·면 연결과 복사 텍스처 상대 경로를 검증합니다."""

    if tuple(material.name for material in parsed.materials) != tuple(
        material.name for material in snapshot.materials
    ):
        raise ValueError("OBJ 출력 재질 이름 또는 순서가 snapshot과 다릅니다.")
    if not np.array_equal(
        parsed.triangle_material_indices_int32,
        snapshot.triangle_material_indices_int32,
    ):
        raise ValueError("OBJ 출력 face 재질 연결이 snapshot과 다릅니다.")
    destination_root = Path(output_root)
    for expected, actual in zip(snapshot.materials, parsed.materials, strict=True):
        if not np.allclose(actual.diffuse_color, expected.diffuse_color, rtol=0.0, atol=1.0e-12):
            raise ValueError(f"OBJ 출력 diffuse 색상이 snapshot과 다릅니다: {expected.name}")
        asset = snapshot.texture_asset_for_material(expected)
        if asset is None:
            if actual.diffuse_texture_path is not None:
                raise ValueError(f"OBJ 출력에 예상하지 않은 diffuse 텍스처가 있습니다: {expected.name}")
            continue
        destination = (destination_root / asset.output_relative_path).resolve(strict=False)
        if actual.diffuse_texture_path != destination or not destination.is_file():
            raise ValueError(f"OBJ 출력 diffuse 텍스처 연결이 snapshot과 다릅니다: {expected.name}")


def validate_obj_material_roundtrip(
    parsed: Any,
    snapshot: ObjExportModelSnapshot,
    output_root: str | Path,
) -> tuple[tuple[str, str], ...]:
    """왕복 파싱 재질 연결과 복사 텍스처의 원본 동일성을 검증합니다."""

    validate_obj_material_links(parsed, snapshot, output_root)
    destination_root = Path(output_root)
    copied: list[tuple[str, str]] = []
    for asset in snapshot.texture_assets:
        source_stat = asset.source_path.stat()
        if (
            int(source_stat.st_size) != asset.source_size
            or int(source_stat.st_mtime_ns) != asset.source_mtime_ns
        ):
            raise ValueError(f"OBJ 텍스처 원본이 내보내기 중 변경되었습니다: {asset.source_path}")
        destination = destination_root / asset.output_relative_path
        source_hash = sha256_file(asset.source_path)
        destination_hash = sha256_file(destination)
        if source_hash != destination_hash:
            raise ValueError(f"OBJ 텍스처 복사본이 원본 바이트와 다릅니다: {destination}")
        copied.append((asset.output_relative_path.as_posix(), destination_hash))
    return tuple(copied)


def validate_obj_roundtrip(
    path: str | Path,
    snapshot: ObjExportModelSnapshot,
    *,
    tolerance: float = 1.0e-12,
) -> ObjRoundTripValidation:
    """기록한 OBJ를 다시 읽어 형상·UV·재질·텍스처 연결을 검증합니다."""

    try:
        parsed = read_obj(path)
    except (ObjReaderError, OSError, ValueError) as error:
        raise ValueError(f"OBJ 출력 왕복 검증에 실패했습니다: {path}") from error
    if (
        parsed.vertex_count != snapshot.vertex_count
        or parsed.triangle_count != snapshot.face_count
        or parsed.texture_coordinate_count != snapshot.texture_coordinate_count
    ):
        raise ValueError("OBJ 출력 정점·UV·삼각면 수가 snapshot과 다릅니다.")
    expected_vertices = transformed_vertices(snapshot)
    if not np.allclose(parsed.vertices_float64, expected_vertices, rtol=0.0, atol=float(tolerance)):
        raise ValueError("OBJ 출력 좌표가 Project World 변환 결과와 다릅니다.")
    expected_faces = transformed_faces(snapshot) - 1
    if not np.array_equal(parsed.triangles_uint32.astype(np.uint64, copy=False), expected_faces):
        raise ValueError("OBJ 출력 face 연결 또는 winding이 snapshot과 다릅니다.")
    if not np.array_equal(
        parsed.texcoord_component_counts_uint8,
        snapshot.texcoord_component_counts_uint8,
    ):
        raise ValueError("OBJ 출력 UV 성분 수가 snapshot과 다릅니다.")
    if not np.allclose(
        parsed.texcoords_float64,
        snapshot.texcoords_float64,
        rtol=0.0,
        atol=float(tolerance),
    ):
        raise ValueError("OBJ 출력 UV 좌표가 snapshot과 다릅니다.")
    expected_face_texcoords = transformed_face_texcoord_indices(snapshot)
    if not np.array_equal(
        parsed.triangle_texcoord_indices_int32,
        expected_face_texcoords,
    ):
        raise ValueError("OBJ 출력 face UV 연결 또는 winding이 snapshot과 다릅니다.")
    copied_texture_sha256 = validate_obj_material_roundtrip(parsed, snapshot, Path(path).parent)
    return ObjRoundTripValidation(
        vertex_count=parsed.vertex_count,
        face_count=parsed.triangle_count,
        texture_coordinate_count=parsed.texture_coordinate_count,
        textured_face_count=parsed.textured_face_count,
        uv_coverage=(parsed.textured_face_count / parsed.triangle_count),
        bounds_min=tuple(float(value) for value in parsed.bounds_min),
        bounds_max=tuple(float(value) for value in parsed.bounds_max),
        output_sha256=sha256_file(path),
        material_count=parsed.material_count,
        diffuse_texture_count=parsed.diffuse_texture_count,
        copied_texture_sha256=copied_texture_sha256,
    )


def write_obj_incremental(
    path: str | Path,
    snapshot: ObjExportModelSnapshot,
    *,
    vertex_chunk_size: int = _DEFAULT_VERTEX_CHUNK_SIZE,
    texcoord_chunk_size: int = _DEFAULT_TEXCOORD_CHUNK_SIZE,
    face_chunk_size: int = _DEFAULT_FACE_CHUNK_SIZE,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> ObjRoundTripValidation:
    """동기 경계에서 OBJ·MTL·원본 텍스처를 기록하고 전체 왕복 검증합니다."""

    vertex_chunk = max(1, int(vertex_chunk_size))
    texcoord_chunk = max(1, int(texcoord_chunk_size))
    face_chunk = max(1, int(face_chunk_size))
    with IncrementalObjWriter(path, snapshot) as writer:
        for begin in range(0, snapshot.vertex_count, vertex_chunk):
            if cancel_check is not None and cancel_check():
                raise RuntimeError("OBJ 내보내기가 취소되었습니다.")
            writer.write_vertices(begin, min(snapshot.vertex_count, begin + vertex_chunk))
            if progress_callback is not None:
                progress_callback(writer._vertex_offset, writer._face_offset)
        for begin in range(0, snapshot.texture_coordinate_count, texcoord_chunk):
            if cancel_check is not None and cancel_check():
                raise RuntimeError("OBJ 내보내기가 취소되었습니다.")
            writer.write_texture_coordinates(
                begin,
                min(snapshot.texture_coordinate_count, begin + texcoord_chunk),
            )
            if progress_callback is not None:
                progress_callback(writer._vertex_offset, writer._face_offset)
        for begin in range(0, snapshot.face_count, face_chunk):
            if cancel_check is not None and cancel_check():
                raise RuntimeError("OBJ 내보내기가 취소되었습니다.")
            writer.write_faces(begin, min(snapshot.face_count, begin + face_chunk))
            if progress_callback is not None:
                progress_callback(writer._vertex_offset, writer._face_offset)
    write_material_library(Path(path).with_name(snapshot.material_library_filename), snapshot)
    copy_texture_assets(
        Path(path).parent,
        snapshot,
        cancel_check=cancel_check,
    )
    return validate_obj_roundtrip(path, snapshot)


def build_obj_export_report(
    *,
    job_id: str,
    snapshots: Iterable[ObjExportModelSnapshot],
    validations: dict[str, ObjRoundTripValidation],
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """검증된 모델별 형상·UV·재질·텍스처 결과를 canonical 보고서로 만듭니다."""

    ordered = tuple(snapshots)
    report = {
        "schema_version": 3,
        "job_id": str(job_id),
        "created_at_utc": created_at_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "coordinate_space": "PROJECT_WORLD",
        "model_count": len(ordered),
        "total_vertex_count": sum(snapshot.vertex_count for snapshot in ordered),
        "total_texture_coordinate_count": sum(
            snapshot.texture_coordinate_count for snapshot in ordered
        ),
        "total_textured_face_count": sum(snapshot.textured_face_count for snapshot in ordered),
        "total_material_count": sum(snapshot.material_count for snapshot in ordered),
        "total_diffuse_texture_count": sum(snapshot.diffuse_texture_count for snapshot in ordered),
        "total_face_count": sum(snapshot.face_count for snapshot in ordered),
        "models": [
            {
                "model_id": snapshot.model_id,
                "display_name": snapshot.display_name,
                "output_filename": snapshot.output_filename,
                "vertex_count": snapshot.vertex_count,
                "texture_coordinate_count": snapshot.texture_coordinate_count,
                "textured_face_count": snapshot.textured_face_count,
                "uv_coverage": snapshot.uv_coverage,
                "material_count": snapshot.material_count,
                "diffuse_texture_count": snapshot.diffuse_texture_count,
                "material_library_filename": (
                    snapshot.material_library_filename if snapshot.material_count else None
                ),
                "copied_textures": [
                    {"relative_path": relative_path, "sha256": sha256}
                    for relative_path, sha256 in validations[
                        snapshot.model_id
                    ].copied_texture_sha256
                ],
                "face_count": snapshot.face_count,
                "source_fingerprint": snapshot.source_fingerprint,
                "display_fingerprint": snapshot.display_fingerprint,
                "alignment_fingerprint": snapshot.alignment_fingerprint,
                "output_sha256": validations[snapshot.model_id].output_sha256,
            }
            for snapshot in ordered
        ],
    }
    return report


def write_json_deterministic(path: str | Path, value: dict[str, Any]) -> None:
    """OBJ 보고서를 UTF-8 정렬 key와 개행으로 원자 기록합니다."""

    target = Path(path)
    temporary = target.with_name(f".{target.name}.tmp")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": ")) + "\n"
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    with temporary.open("rb") as stream:
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, target)


__all__ = (
    "IncrementalObjWriter",
    "ObjExportModelSnapshot",
    "ObjRoundTripValidation",
    "ObjTextureAssetSnapshot",
    "build_obj_export_report",
    "canonical_obj_filename",
    "compose_project_world_matrix",
    "copy_texture_assets",
    "material_library_text",
    "model_id_short_hash",
    "sanitize_obj_name",
    "sha256_file",
    "transformed_faces",
    "transformed_face_texcoord_indices",
    "transformed_vertices",
    "validate_obj_roundtrip",
    "validate_obj_material_links",
    "validate_windows_output_path",
    "validate_obj_material_roundtrip",
    "write_json_deterministic",
    "write_material_library",
    "write_obj_incremental",
)
