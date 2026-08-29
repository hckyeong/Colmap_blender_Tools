"""OBJ의 MTL diffuse 재질과 원본 텍스처 경로를 안전하게 해석합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex

import numpy as np

from .types import ObjMaterialData


class ObjMaterialError(ValueError):
    """MTL 문법이나 diffuse 텍스처 경로를 해석할 수 없을 때 사용합니다."""


@dataclass(slots=True)
class _MaterialBuilder:
    """MTL 한 재질을 읽는 동안 변경 가능한 diffuse 속성을 누적합니다."""

    name: str
    source_path: Path
    diffuse_color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    diffuse_texture_path: Path | None = None


def split_obj_arguments(raw: str) -> tuple[str, ...]:
    """OBJ·MTL 지시문 인수를 따옴표와 Windows 역슬래시를 보존해 분리합니다."""

    lexer = shlex.shlex(str(raw), posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = tuple(lexer)
    except ValueError as error:
        raise ObjMaterialError("OBJ/MTL 인수의 따옴표가 닫히지 않았습니다.") from error
    normalized = []
    for token in tokens:
        value = str(token)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if "\x00" in value:
            raise ObjMaterialError("OBJ/MTL 경로에 NUL 문자가 있습니다.")
        normalized.append(value)
    return tuple(normalized)


def _finite_float(token: str, label: str) -> float:
    """MTL 숫자 토큰을 유한한 float 값으로 변환합니다."""

    try:
        value = float(token)
    except (TypeError, ValueError) as error:
        raise ObjMaterialError(f"MTL {label} 값이 숫자가 아닙니다: {token}") from error
    if not np.isfinite(value):
        raise ObjMaterialError(f"MTL {label} 값에 NaN 또는 Infinity가 있습니다.")
    return value


def _map_texture_argument(raw: str) -> str:
    """map_Kd 옵션을 건너뛰고 마지막 diffuse 이미지 경로를 반환합니다."""

    tokens = split_obj_arguments(raw)
    single_value_options = {
        "-blendu",
        "-blendv",
        "-boost",
        "-bm",
        "-cc",
        "-clamp",
        "-imfchan",
        "-texres",
        "-type",
    }
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index].casefold()
        index += 1
        if option == "-mm":
            index += 2
            continue
        if option in {"-o", "-s", "-t"}:
            consumed = 0
            while index < len(tokens) and consumed < 3:
                try:
                    float(tokens[index])
                except ValueError:
                    break
                index += 1
                consumed += 1
            if consumed == 0:
                raise ObjMaterialError(f"MTL {option} 옵션 값이 비어 있습니다.")
            continue
        if option in single_value_options:
            index += 1
            continue
        raise ObjMaterialError(f"지원하지 않는 MTL map_Kd 옵션입니다: {option}")
    if index >= len(tokens):
        raise ObjMaterialError("MTL map_Kd 이미지 경로가 비어 있습니다.")
    value = " ".join(tokens[index:]).strip()
    if not value:
        raise ObjMaterialError("MTL map_Kd 이미지 경로가 비어 있습니다.")
    return value


def _parse_material_library(path: Path) -> dict[str, ObjMaterialData]:
    """존재하는 MTL 하나에서 newmtl·Kd·map_Kd 정의를 읽습니다."""

    builders: dict[str, _MaterialBuilder] = {}
    current: _MaterialBuilder | None = None
    try:
        stream = path.open("r", encoding="utf-8-sig", errors="strict")
    except OSError as error:
        raise ObjMaterialError(f"MTL 파일을 열 수 없습니다: {path}") from error
    try:
        for line_number, raw_line in enumerate(stream, start=1):
            stripped = raw_line.split("#", 1)[0].strip()
            if not stripped:
                continue
            fields = stripped.split(None, 1)
            command = fields[0].casefold()
            arguments = fields[1].strip() if len(fields) == 2 else ""
            if command == "newmtl":
                name = arguments
                if not name:
                    raise ObjMaterialError(f"MTL newmtl 이름이 비어 있습니다 (line {line_number}).")
                current = _MaterialBuilder(name=name, source_path=path)
                builders[name] = current
                continue
            if current is None:
                continue
            if command == "kd":
                values = split_obj_arguments(arguments)
                if len(values) != 3:
                    raise ObjMaterialError(f"MTL Kd는 RGB 세 값이어야 합니다 (line {line_number}).")
                current.diffuse_color = tuple(
                    _finite_float(value, "Kd") for value in values
                )
            elif command == "map_kd":
                texture_reference = _map_texture_argument(arguments)
                candidate = Path(texture_reference).expanduser()
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                current.diffuse_texture_path = candidate.resolve(strict=False)
    except UnicodeError as error:
        raise ObjMaterialError(f"MTL 파일 인코딩을 읽을 수 없습니다: {path}") from error
    finally:
        stream.close()
    return {
        name: ObjMaterialData(
            name=builder.name,
            diffuse_color=builder.diffuse_color,
            diffuse_texture_path=builder.diffuse_texture_path,
            source_material_path=builder.source_path,
        )
        for name, builder in builders.items()
    }


def resolve_obj_materials(
    source_obj_path: Path,
    material_library_references: list[str],
    face_material_names: list[str | None],
) -> tuple[tuple[ObjMaterialData, ...], np.ndarray]:
    """OBJ 재질 라이브러리를 합치고 각 면 이름을 불변 재질 인덱스로 변환합니다."""

    definitions: dict[str, ObjMaterialData] = {}
    resolved_libraries: set[Path] = set()
    for reference in material_library_references:
        candidate = Path(reference).expanduser()
        if not candidate.is_absolute():
            candidate = source_obj_path.parent / candidate
        material_path = candidate.resolve(strict=False)
        if material_path in resolved_libraries or not material_path.is_file():
            continue
        resolved_libraries.add(material_path)
        definitions.update(_parse_material_library(material_path))

    materials: list[ObjMaterialData] = []
    material_indices: dict[str, int] = {}
    face_indices = np.full((len(face_material_names),), -1, dtype=np.int32)
    for face_index, material_name in enumerate(face_material_names):
        if material_name is None:
            continue
        index = material_indices.get(material_name)
        if index is None:
            material = definitions.get(material_name, ObjMaterialData(name=material_name))
            index = len(materials)
            materials.append(material)
            material_indices[material_name] = index
        face_indices[face_index] = index
    face_indices.setflags(write=False)
    return tuple(materials), face_indices


__all__ = (
    "ObjMaterialError",
    "resolve_obj_materials",
    "split_obj_arguments",
)
