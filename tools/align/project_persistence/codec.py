"""버전화된 프로젝트 상태를 엄격한 canonical JSON으로 변환합니다."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from math import isfinite
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from ..alignment.types import AlignmentProvenanceRecord, Sim3Transform
from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_GEOMETRY_SOURCE_COMPANION,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_GEOMETRY_SOURCES,
    MARKER_POINT_KIND_COLMAP,
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KIND_MESH_VERTEX,
    MARKER_POINT_KINDS,
    MODEL_STATUS_PENDING,
    MODEL_STATUS_REGISTERED,
    PROJECT_PERSISTENCE_MAX_JSON_BYTES,
    PROJECT_PERSISTENCE_MAX_MARKERS,
    PROJECT_PERSISTENCE_MAX_MODELS,
    PROJECT_PERSISTENCE_SCHEMA_VERSION,
    PROJECT_PERSISTENCE_SUPPORTED_SCHEMA_VERSIONS,
    PROJECT_PERSISTENCE_MAX_ID_LENGTH,
    PROJECT_PERSISTENCE_MAX_NAME_LENGTH,
    PROJECT_PERSISTENCE_MAX_PATH_LENGTH,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from .types import (
    ProjectAxesSnapshot,
    ProjectCatalogSnapshot,
    ProjectCompanionSourceSnapshot,
    ProjectMarkerPairSnapshot,
    ProjectMarkerPointSnapshot,
    ProjectMarkerSnapshot,
    ProjectModelSnapshot,
    ProjectSourceSnapshot,
    ProjectStateSnapshot,
)


class ProjectPersistenceCodecError(ValueError):
    """저장 문서 구조·무결성·지원 버전이 계약과 다를 때 발생합니다."""


def _canonical_json_bytes(value: object) -> bytes:
    """SHA-256 계산과 Text 기록에 공통으로 쓸 canonical UTF-8 JSON을 만듭니다."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProjectPersistenceCodecError("프로젝트 상태를 JSON으로 변환할 수 없습니다.") from error
    if len(encoded) > PROJECT_PERSISTENCE_MAX_JSON_BYTES:
        raise ProjectPersistenceCodecError("프로젝트 상태 JSON 크기가 16 MiB 제한을 초과했습니다.")
    return encoded


def _provenance_mapping(
    value: AlignmentProvenanceRecord | None,
    schema_version: int,
) -> dict[str, object] | None:
    """마커·정밀 형상과 엔진 출처를 구버전 지문을 유지하며 저장합니다."""

    if value is None:
        return None
    mapping = value.to_mapping()
    if schema_version < 6 or not value.marker_geometry_dependencies:
        mapping.pop("marker_geometry_dependencies", None)
    if schema_version < 7:
        mapping.pop("solver_geometry_dependencies", None)
        mapping.pop("engine_metadata", None)
    return mapping


def _source_mapping(source: ProjectSourceSnapshot, schema_version: int) -> dict[str, object]:
    """원본 출처를 버전에 맞게 변환하고 v4에서는 데이터셋 루트를 보존합니다."""

    path_absolute = source.path_absolute
    path_relative = source.path_relative
    if source.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD:
        # v1 호환 객체가 sparse 별칭만 수정한 경우에도 저장된 변경을 보존합니다.
        path_absolute = source.sparse_path_absolute or path_absolute
        path_relative = source.sparse_path_relative
    mapping = {
        "kind": source.kind,
        "path_absolute": path_absolute,
        "path_relative": path_relative,
        "source_format": source.source_format,
        "source_fingerprint": source.source_fingerprint,
        "display_vertex_limit": source.display_vertex_limit,
        "axis_preset": source.axis_preset,
        "axis_forward": source.axis_forward,
        "axis_up": source.axis_up,
        "display_transform_fingerprint": source.display_transform_fingerprint,
    }
    if int(schema_version) >= 4:
        mapping["selected_root_absolute"] = source.selected_root_absolute
        mapping["selected_root_relative"] = source.selected_root_relative
    return mapping


def _companion_source_mapping(
    source: ProjectCompanionSourceSnapshot,
    schema_version: int = PROJECT_PERSISTENCE_SCHEMA_VERSION,
) -> dict[str, object]:
    """연결 OBJ의 출처와 v8 내장 Mesh 식별자를 JSON 객체로 변환합니다."""

    mapping = {
        "path_absolute": source.path_absolute,
        "path_relative": source.path_relative,
        "source_fingerprint": source.source_fingerprint,
        "geometry_only": source.geometry_only,
    }
    if int(schema_version) >= 8:
        mapping.update(
            {
                "embedded_obj_asset_uuid": source.embedded_obj_asset_uuid,
                "embedded_obj_schema_version": source.embedded_obj_schema_version,
                "embedded_obj_data_name_hint": source.embedded_obj_data_name_hint,
                "embedded_obj_geometry_revision": source.embedded_obj_geometry_revision,
            }
        )
    return mapping


def _axes_mapping(axes: ProjectAxesSnapshot, schema_version: int = PROJECT_PERSISTENCE_SCHEMA_VERSION) -> dict[str, object]:
    """확정 표시 변환을 저장 버전에 맞게 기록하고 구버전에서의 변환 손실을 차단합니다."""

    mapping = {
        "preset_id": axes.preset_id,
        "axis_forward": axes.axis_forward,
        "axis_up": axes.axis_up,
        "rotation_xyz_degrees": axes.rotation_xyz_degrees,
        "display_transform_fingerprint": axes.display_transform_fingerprint,
    }
    extra = {"translation_xyz": axes.translation_xyz, "scale_xyz": axes.scale_xyz, "pivot_source": axes.pivot_source}
    if int(schema_version) >= 9:
        mapping.update(extra)
    elif any(tuple(extra[key]) != default for key, default in (
        ("translation_xyz", (0.0, 0.0, 0.0)), ("scale_xyz", (1.0, 1.0, 1.0)), ("pivot_source", (0.0, 0.0, 0.0)),
    )):
        raise ProjectPersistenceCodecError("이동·배율·중심 설정은 프로젝트 저장 버전 9 이상이 필요합니다.")
    return mapping


def _axes_from_source(source: ProjectSourceSnapshot) -> ProjectAxesSnapshot:
    """구버전 화면을 보존하도록 최초 가져오기 축을 0도 현재 축으로 이관합니다."""

    return ProjectAxesSnapshot(
        preset_id=source.axis_preset,
        axis_forward=source.axis_forward,
        axis_up=source.axis_up,
        rotation_xyz_degrees=(0.0, 0.0, 0.0),
        display_transform_fingerprint=source.display_transform_fingerprint,
    )


def _legacy_geometry_catalog(
    model: ProjectModelSnapshot,
    markers: ProjectMarkerSnapshot,
) -> ProjectCatalogSnapshot:
    """이전 표시 토글을 완료 마커 종류와 실제 형상에 맞춰 단일 선택으로 이관합니다."""

    catalog = model.catalog
    both_hidden = not catalog.show_points and not catalog.show_surface
    if model.source.geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH:
        show_points, show_surface = False, True
    elif model.companion_source is None:
        show_points, show_surface = True, False
    elif catalog.show_points != catalog.show_surface:
        show_points, show_surface = catalog.show_points, catalog.show_surface
    else:
        marker_kinds = {
            "POINTS" if point.point_kind == MARKER_POINT_KIND_COLMAP else "MESH"
            for pair in markers.completed_pairs
            for point in (pair.reference, pair.moving)
            if point.model_id == model.model_id
        }
        show_points = marker_kinds == {"POINTS"}
        show_surface = not show_points
    return replace(
        catalog,
        show_points=show_points,
        show_surface=show_surface,
        is_visible=catalog.is_visible and not both_hidden,
    )


def _upgrade_snapshot(snapshot: ProjectStateSnapshot) -> ProjectStateSnapshot:
    """기존 축·정렬·마커를 보존하고 구버전 표시 토글만 현재 단일 선택으로 이관합니다."""

    models = tuple(
        replace(
            model,
            catalog=(
                _legacy_geometry_catalog(model, snapshot.markers)
                if snapshot.schema_version < 6 else model.catalog
            ),
            current_axes=model.current_axes or _axes_from_source(model.source),
            companion_axes=(
                model.companion_axes or model.current_axes or _axes_from_source(model.source)
                if model.companion_source is not None else None
            ),
        )
        for model in snapshot.models
    )
    return replace(snapshot, schema_version=PROJECT_PERSISTENCE_SCHEMA_VERSION, models=models)


def _model_mapping(model: ProjectModelSnapshot, schema_version: int) -> dict[str, object]:
    """모델 출처·표시 상태와 버전별 형상 변환을 저장 객체로 변환합니다."""

    mapping = {
        "model_id": model.model_id,
        "display_name": model.display_name,
        "load_order": model.load_order,
        "geometry_kind": model.source.geometry_kind,
        "source": _source_mapping(model.source, schema_version),
        "catalog": {
            "registration_status": model.catalog.registration_status,
            "is_anchor": model.catalog.is_anchor,
            "is_visible": model.catalog.is_visible,
            "alignment_provenance": _provenance_mapping(model.catalog.alignment_provenance, schema_version),
        },
        "alignment": {
            "matrix_float64": [list(row) for row in model.alignment_matrix_float64],
        },
    }
    if int(schema_version) >= 4:
        mapping["catalog"]["show_points"] = model.catalog.show_points
        mapping["catalog"]["show_surface"] = model.catalog.show_surface
        if model.companion_source is not None:
            mapping["companion_source"] = _companion_source_mapping(model.companion_source, schema_version)
    if int(schema_version) >= 5:
        mapping["current_axes"] = _axes_mapping(model.current_axes, schema_version)
        if model.companion_axes is not None:
            mapping["companion_axes"] = _axes_mapping(model.companion_axes, schema_version)
    if int(schema_version) >= 8:
        mapping.update(
            {
                "embedded_obj_asset_uuid": model.embedded_obj_asset_uuid,
                "embedded_obj_schema_version": model.embedded_obj_schema_version,
                "embedded_obj_data_name_hint": model.embedded_obj_data_name_hint,
                "embedded_obj_geometry_revision": model.embedded_obj_geometry_revision,
            }
        )
    return mapping


def _marker_point_mapping(
    point: ProjectMarkerPointSnapshot,
    schema_version: int,
) -> dict[str, object]:
    """마커의 원본 참조·선택 좌표와 v6 형상 출처를 저장 객체로 변환합니다."""

    mapping: dict[str, object] = {
        "model_role": point.model_role,
        "model_id": point.model_id,
        "point_kind": point.point_kind,
        "source_point_id": point.source_point_id,
    }
    if int(schema_version) >= 6:
        mapping["geometry_source"] = point.geometry_source
    if int(schema_version) >= 3:
        mapping["local_position"] = (
            None
            if point.local_position is None
            else list(point.local_position)
        )
    return mapping


def _snapshot_mapping(snapshot: ProjectStateSnapshot, *, include_fingerprint: bool) -> dict[str, object]:
    """전체 snapshot을 fingerprint 포함 여부가 명시된 schema 객체로 변환합니다."""

    mapping: dict[str, object] = {
        "schema_version": snapshot.schema_version,
        "writer_addon_version": list(snapshot.writer_addon_version),
        "project_uuid": snapshot.project_uuid,
        "saved_at_utc": snapshot.saved_at_utc,
        "models": [_model_mapping(model, snapshot.schema_version) for model in snapshot.models],
        "markers": {
            "next_pair_id": snapshot.markers.next_pair_id,
            "active_pair_id": snapshot.markers.active_pair_id,
            "completed_pairs": [
                {
                    "pair_id": pair.pair_id,
                    "reference": _marker_point_mapping(pair.reference, snapshot.schema_version),
                    "moving": _marker_point_mapping(pair.moving, snapshot.schema_version),
                }
                for pair in snapshot.markers.completed_pairs
            ],
        },
    }
    if include_fingerprint:
        mapping["payload_fingerprint"] = snapshot.payload_fingerprint
    return mapping


def payload_fingerprint_for_snapshot(snapshot: ProjectStateSnapshot) -> str:
    """payload_fingerprint 필드를 제외한 canonical JSON의 SHA-256을 계산합니다."""

    if not isinstance(snapshot, ProjectStateSnapshot):
        raise ProjectPersistenceCodecError("프로젝트 상태 snapshot 형식이 올바르지 않습니다.")
    return hashlib.sha256(_canonical_json_bytes(_snapshot_mapping(snapshot, include_fingerprint=False))).hexdigest()


def encode_project_state(snapshot: ProjectStateSnapshot) -> str:
    """호환 스냅샷을 현재 v8 JSON으로 올리고 전체 내용 지문을 포함합니다."""

    _validate_snapshot_semantics(snapshot)
    snapshot = _upgrade_snapshot(snapshot)
    _validate_snapshot_semantics(snapshot)
    fingerprint = payload_fingerprint_for_snapshot(snapshot)
    stored = replace(snapshot, payload_fingerprint=fingerprint)
    return _canonical_json_bytes(_snapshot_mapping(stored, include_fingerprint=True)).decode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON 객체의 중복 키를 조용한 덮어쓰기 없이 즉시 거부합니다."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectPersistenceCodecError(f"프로젝트 상태 JSON에 중복 키가 있습니다: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """NaN·Infinity 같은 JSON 외 상수를 명시적으로 거부합니다."""

    raise ProjectPersistenceCodecError(f"프로젝트 상태 JSON에 허용되지 않는 수치가 있습니다: {value}")


def _decode_json(payload: str | bytes) -> dict[str, object]:
    """크기 제한과 중복 키 검사를 거쳐 최상위 JSON 객체를 읽습니다."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not raw:
        raise ProjectPersistenceCodecError("프로젝트 상태 Text가 비어 있습니다.")
    if len(raw) > PROJECT_PERSISTENCE_MAX_JSON_BYTES:
        raise ProjectPersistenceCodecError("프로젝트 상태 JSON 크기가 16 MiB 제한을 초과했습니다.")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ProjectPersistenceCodecError) as error:
        if isinstance(error, ProjectPersistenceCodecError):
            raise
        raise ProjectPersistenceCodecError("프로젝트 상태 Text의 JSON 형식이 올바르지 않습니다.") from error
    return _mapping(value, "프로젝트 상태 최상위")


def project_state_schema_version(payload: str | bytes) -> int:
    """엄격하게 읽은 프로젝트 JSON의 원본 schema_version을 반환합니다."""

    mapping = _decode_json(payload)
    if "schema_version" not in mapping:
        raise ProjectPersistenceCodecError("프로젝트 상태 schema_version이 누락되었습니다.")
    return _integer(mapping["schema_version"], "schema_version", minimum=1)


def _mapping(value: object, field_name: str) -> dict[str, object]:
    """JSON 객체가 아닌 값을 필드별 오류로 바꿔 반환합니다."""

    if not isinstance(value, dict):
        raise ProjectPersistenceCodecError(f"{field_name}은 JSON 객체여야 합니다.")
    return value


def _list(value: object, field_name: str) -> list[object]:
    """JSON 배열 타입을 필드별 오류로 검증해 반환합니다."""

    if not isinstance(value, list):
        raise ProjectPersistenceCodecError(f"{field_name}은 JSON 배열이어야 합니다.")
    return value


def _require_keys(mapping: dict[str, object], keys: set[str], field_name: str) -> None:
    """schema 객체의 누락·알 수 없는 키를 함께 거부합니다."""

    actual = set(mapping)
    if actual != keys:
        missing = ", ".join(sorted(keys - actual))
        unknown = ", ".join(sorted(actual - keys))
        details = "; ".join(
            item
            for item in (
                f"누락: {missing}" if missing else "",
                f"알 수 없음: {unknown}" if unknown else "",
            )
            if item
        )
        raise ProjectPersistenceCodecError(f"{field_name} 필드가 schema와 다릅니다 ({details}).")


def _string(value: object, field_name: str, maximum: int, *, allow_empty: bool = False) -> str:
    """길이 제한을 포함해 문자열 필드를 정규화합니다."""

    if not isinstance(value, str):
        raise ProjectPersistenceCodecError(f"{field_name}은 문자열이어야 합니다.")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ProjectPersistenceCodecError(f"{field_name}은 비어 있을 수 없습니다.")
    if len(normalized) > maximum:
        raise ProjectPersistenceCodecError(f"{field_name} 길이가 제한을 초과했습니다.")
    return normalized


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    """bool을 제외한 정수 필드를 하한과 함께 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProjectPersistenceCodecError(f"{field_name}은 {minimum} 이상의 정수여야 합니다.")
    return value


def _boolean(value: object, field_name: str) -> bool:
    """정확한 JSON bool만 허용해 상태 플래그를 읽습니다."""

    if not isinstance(value, bool):
        raise ProjectPersistenceCodecError(f"{field_name}은 bool이어야 합니다.")
    return value


def _finite_float(value: object, field_name: str) -> float:
    """JSON 수치를 유한한 float64 후보로 변환합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectPersistenceCodecError(f"{field_name}은 숫자여야 합니다.")
    numeric = float(value)
    if not isfinite(numeric):
        raise ProjectPersistenceCodecError(f"{field_name}은 유한한 숫자여야 합니다.")
    return numeric


def _optional_position(
    value: object,
    field_name: str,
) -> tuple[float, float, float] | None:
    """nullable JSON 좌표를 유한한 float64 세 항목 tuple로 검증합니다."""

    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ProjectPersistenceCodecError(f"{field_name}은 JSON 배열이어야 합니다.")
    values = value
    if len(values) != 3:
        raise ProjectPersistenceCodecError(f"{field_name}는 세 항목이어야 합니다.")
    return tuple(_finite_float(item, field_name) for item in values)


def _hex_fingerprint(value: object, field_name: str, length: int) -> str:
    """고정 길이 16진수 fingerprint를 문자열·길이·문자 경계에서 검증합니다."""

    fingerprint = _string(value, field_name, length)
    if len(fingerprint) != length or any(
        character not in "0123456789abcdef" for character in fingerprint.lower()
    ):
        raise ProjectPersistenceCodecError(f"{field_name}은 {length}자리 16진수여야 합니다.")
    return fingerprint


def _decode_provenance(
    value: object, model_id: str, schema_version: int,
) -> AlignmentProvenanceRecord | None:
    """구버전 감사 기록과 v7 정밀 형상·엔진 출처를 원본 참조로 복원합니다."""

    if value is None:
        return None
    mapping = _mapping(value, "alignment_provenance")
    expected = {
        "model_id",
        "method",
        "backend_id",
        "algorithm_version",
        "input_fingerprint",
        "result_fingerprint",
        "marker_rms",
        "marker_maximum_error",
        "icp_fitness",
        "icp_rmse",
        "icp_status",
        "committed_at_utc",
    }
    dependencies = ()
    if schema_version >= 6 and "marker_geometry_dependencies" in mapping:
        expected.add("marker_geometry_dependencies")
        raw_dependencies = _list(mapping["marker_geometry_dependencies"], "alignment_provenance.marker_geometry_dependencies")
        if len(raw_dependencies) > PROJECT_PERSISTENCE_MAX_MODELS * 2:
            raise ProjectPersistenceCodecError("마커 형상 의존성 수가 모델 형상 수 제한을 초과했습니다.")
        items = []
        for raw_dependency in raw_dependencies:
            dependency = _list(raw_dependency, "marker_geometry_dependencies 항목")
            if len(dependency) != 2:
                raise ProjectPersistenceCodecError("마커 형상 의존성은 모델 ID와 형상 출처 쌍이어야 합니다.")
            owner_id = _string(dependency[0], "마커 의존 모델 ID", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
            source = _string(dependency[1], "마커 의존 형상 출처", 32).upper()
            if source not in MARKER_GEOMETRY_SOURCES:
                raise ProjectPersistenceCodecError("마커 의존 형상 출처가 올바르지 않습니다.")
            items.append((owner_id, source))
        if len(items) != len(set(items)):
            raise ProjectPersistenceCodecError("마커 형상 의존성이 중복되었습니다.")
        dependencies = tuple(items)
    solver_dependencies = ()
    engine_metadata_json = ""
    if schema_version >= 7:
        if "solver_geometry_dependencies" in mapping:
            expected.add("solver_geometry_dependencies")
            raw = _list(mapping["solver_geometry_dependencies"], "정밀 정렬 형상 출처")
            if len(raw) > PROJECT_PERSISTENCE_MAX_MODELS * 2:
                raise ProjectPersistenceCodecError("정밀 정렬 형상 출처 수가 제한을 초과했습니다.")
            decoded = []
            for item in raw:
                pair = _list(item, "정밀 정렬 형상 출처 항목")
                if len(pair) != 2:
                    raise ProjectPersistenceCodecError("정밀 정렬 형상 출처는 소유자와 출처 쌍이어야 합니다.")
                owner = _string(pair[0], "정밀 형상 소유자", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
                source = _string(pair[1], "정밀 형상 출처", 32)
                if source not in MARKER_GEOMETRY_SOURCES:
                    raise ProjectPersistenceCodecError("정밀 정렬 형상 출처가 올바르지 않습니다.")
                decoded.append((owner, source))
            if len(decoded) != len(set(decoded)):
                raise ProjectPersistenceCodecError("정밀 정렬 형상 출처가 중복되었습니다.")
            solver_dependencies = tuple(decoded)
        if "engine_metadata" in mapping:
            expected.add("engine_metadata")
            engine_metadata_json = _canonical_json_bytes(
                _mapping(mapping["engine_metadata"], "정렬 엔진 출처")
            ).decode("utf-8")
    _require_keys(mapping, expected, "alignment_provenance")
    if _string(mapping["model_id"], "alignment_provenance.model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH) != model_id:
        raise ProjectPersistenceCodecError("alignment_provenance 모델 ID가 상위 모델과 다릅니다.")
    optional_number = lambda field: (
        None if mapping[field] is None else _finite_float(mapping[field], f"alignment_provenance.{field}")
    )
    try:
        return AlignmentProvenanceRecord(
            model_id=model_id,
            method=_string(mapping["method"], "alignment_provenance.method", 64),
            backend_id=_string(mapping["backend_id"], "alignment_provenance.backend_id", 256, allow_empty=True),
            algorithm_version=_string(mapping["algorithm_version"], "alignment_provenance.algorithm_version", 256, allow_empty=True),
            input_fingerprint=_string(mapping["input_fingerprint"], "alignment_provenance.input_fingerprint", 256, allow_empty=True),
            result_fingerprint=_string(mapping["result_fingerprint"], "alignment_provenance.result_fingerprint", 256, allow_empty=True),
            marker_rms=optional_number("marker_rms"),
            marker_maximum_error=optional_number("marker_maximum_error"),
            icp_fitness=optional_number("icp_fitness"),
            icp_rmse=optional_number("icp_rmse"),
            icp_status=(
                None
                if mapping["icp_status"] is None
                else _string(mapping["icp_status"], "alignment_provenance.icp_status", 64)
            ),
            committed_at_utc=_string(mapping["committed_at_utc"], "alignment_provenance.committed_at_utc", 64),
            marker_geometry_dependencies=dependencies,
            solver_geometry_dependencies=solver_dependencies,
            engine_metadata_json=engine_metadata_json,
        )
    except ValueError as error:
        raise ProjectPersistenceCodecError(f"alignment_provenance가 올바르지 않습니다: {error}") from error


def _decode_source(value: object, schema_version: int) -> ProjectSourceSnapshot:
    """지원 버전의 COLMAP·OBJ 출처와 v4 데이터셋 루트를 검증해 복원합니다."""

    mapping = _mapping(value, "source")
    v2 = "kind" in mapping or "path_absolute" in mapping or "display_vertex_limit" in mapping
    if v2:
        expected = {
            "kind",
            "path_absolute",
            "path_relative",
            "source_format",
            "source_fingerprint",
            "display_vertex_limit",
            "axis_preset",
            "axis_forward",
            "axis_up",
            "display_transform_fingerprint",
        }
    else:
        expected = {
            "selected_root_absolute",
            "sparse_path_absolute",
            "sparse_path_relative",
            "source_format",
            "source_fingerprint",
            "display_point_limit",
            "axis_preset",
            "axis_forward",
            "axis_up",
            "display_transform_fingerprint",
        }
    if int(schema_version) >= 4:
        expected.update({"selected_root_absolute", "selected_root_relative"})
    _require_keys(mapping, expected, "source")
    source_format = _string(mapping["source_format"], "source.source_format", 16).lower()
    if source_format not in {"binary", "text", "obj"}:
        raise ProjectPersistenceCodecError("source.source_format은 binary, text 또는 obj여야 합니다.")
    if v2:
        kind = _string(mapping["kind"], "source.kind", 32).upper()
        if kind not in {"COLMAP", "OBJ"}:
            raise ProjectPersistenceCodecError("source.kind는 COLMAP 또는 OBJ여야 합니다.")
        path_absolute = _string(mapping["path_absolute"], "source.path_absolute", PROJECT_PERSISTENCE_MAX_PATH_LENGTH)
        path_relative = _string(mapping["path_relative"], "source.path_relative", PROJECT_PERSISTENCE_MAX_PATH_LENGTH, allow_empty=True)
        display_limit = _integer(mapping["display_vertex_limit"], "source.display_vertex_limit", minimum=1)
        geometry_kind = GEOMETRY_KIND_TRIANGLE_MESH if kind == "OBJ" else GEOMETRY_KIND_COLMAP_POINT_CLOUD
        if (kind == "OBJ" and source_format != "obj") or (kind == "COLMAP" and source_format == "obj"):
            raise ProjectPersistenceCodecError("source.kind와 source.source_format 조합이 일치하지 않습니다.")
    else:
        path_absolute = _string(mapping["sparse_path_absolute"], "source.sparse_path_absolute", PROJECT_PERSISTENCE_MAX_PATH_LENGTH)
        path_relative = _string(mapping["sparse_path_relative"], "source.sparse_path_relative", PROJECT_PERSISTENCE_MAX_PATH_LENGTH, allow_empty=True)
        display_limit = _integer(mapping["display_point_limit"], "source.display_point_limit", minimum=1)
        geometry_kind = GEOMETRY_KIND_COLMAP_POINT_CLOUD
    fingerprint_raw = _string(
        mapping["source_fingerprint"],
        "source.source_fingerprint",
        64,
        allow_empty=int(schema_version) >= 8 and geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH,
    )
    fingerprint = (
        _hex_fingerprint(fingerprint_raw, "source.source_fingerprint", 64)
        if fingerprint_raw else ""
    )
    transform_fingerprint = _hex_fingerprint(
        mapping["display_transform_fingerprint"],
        "source.display_transform_fingerprint",
        32,
    )
    return ProjectSourceSnapshot(
        selected_root_absolute=(
            _string(mapping["selected_root_absolute"], "source.selected_root_absolute", PROJECT_PERSISTENCE_MAX_PATH_LENGTH)
            if not v2 or int(schema_version) >= 4
            else path_absolute
        ),
        selected_root_relative=(
            _string(mapping["selected_root_relative"], "source.selected_root_relative", PROJECT_PERSISTENCE_MAX_PATH_LENGTH, allow_empty=True)
            if int(schema_version) >= 4 else ""
        ),
        sparse_path_absolute=path_absolute,
        sparse_path_relative=path_relative,
        source_format=source_format,
        source_fingerprint=fingerprint,
        display_point_limit=display_limit,
        axis_preset=_string(mapping["axis_preset"], "source.axis_preset", 128),
        axis_forward=_string(mapping["axis_forward"], "source.axis_forward", 8),
        axis_up=_string(mapping["axis_up"], "source.axis_up", 8),
        display_transform_fingerprint=transform_fingerprint,
        geometry_kind=geometry_kind,
        kind=("OBJ" if geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH else "COLMAP"),
        path_absolute=path_absolute,
        path_relative=path_relative,
        display_vertex_limit=display_limit,
    )


def _decode_alignment(value: object) -> tuple[tuple[float, float, float, float], ...]:
    """4×4 행렬을 읽고 기존 Sim(3) 생성자로 수학적 의미를 검증합니다."""

    mapping = _mapping(value, "alignment")
    _require_keys(mapping, {"matrix_float64"}, "alignment")
    rows = _list(mapping["matrix_float64"], "alignment.matrix_float64")
    if len(rows) != 4:
        raise ProjectPersistenceCodecError("alignment.matrix_float64는 4행이어야 합니다.")
    matrix: list[tuple[float, float, float, float]] = []
    for row_index, row in enumerate(rows):
        values = _list(row, f"alignment.matrix_float64[{row_index}]")
        if len(values) != 4:
            raise ProjectPersistenceCodecError("alignment.matrix_float64의 각 행은 4열이어야 합니다.")
        matrix.append(tuple(_finite_float(item, "alignment.matrix_float64") for item in values))
    try:
        transform = Sim3Transform(matrix_float64=matrix)
    except ValueError as error:
        raise ProjectPersistenceCodecError(f"alignment Sim(3)이 올바르지 않습니다: {error}") from error
    return tuple(tuple(float(item) for item in row) for row in transform.matrix_float64.tolist())


def _decode_companion_source(
    value: object,
    schema_version: int = PROJECT_PERSISTENCE_SCHEMA_VERSION,
) -> ProjectCompanionSourceSnapshot:
    """연결 OBJ 출처와 선택적 내장 Mesh 식별자를 엄격히 검사합니다."""

    mapping = _mapping(value, "companion_source")
    required = {"path_absolute", "path_relative", "source_fingerprint", "geometry_only"}
    if int(schema_version) >= 8:
        required.update(
            {
                "embedded_obj_asset_uuid",
                "embedded_obj_schema_version",
                "embedded_obj_data_name_hint",
                "embedded_obj_geometry_revision",
            }
        )
    _require_keys(mapping, required, "companion_source")
    absolute = _string(mapping["path_absolute"], "companion_source.path_absolute", PROJECT_PERSISTENCE_MAX_PATH_LENGTH)
    relative = _string(mapping["path_relative"], "companion_source.path_relative", PROJECT_PERSISTENCE_MAX_PATH_LENGTH, allow_empty=True)
    if not (PureWindowsPath(absolute).is_absolute() or PurePosixPath(absolute).is_absolute()):
        raise ProjectPersistenceCodecError("연결 OBJ 원본 경로는 절대경로여야 합니다.")
    if PureWindowsPath(absolute).suffix.casefold() != ".obj":
        raise ProjectPersistenceCodecError("연결 표면 원본은 OBJ 파일이어야 합니다.")
    if relative and (PureWindowsPath(relative).is_absolute() or PurePosixPath(relative).is_absolute()):
        raise ProjectPersistenceCodecError("연결 OBJ 상대경로에 절대경로를 사용할 수 없습니다.")
    if not _boolean(mapping["geometry_only"], "companion_source.geometry_only"):
        raise ProjectPersistenceCodecError("연결 OBJ는 형상 전용 읽기만 지원합니다.")
    fingerprint_raw = _string(
        mapping["source_fingerprint"],
        "companion_source.source_fingerprint",
        64,
        allow_empty=int(schema_version) >= 8,
    )
    fingerprint = (
        _hex_fingerprint(fingerprint_raw, "companion_source.source_fingerprint", 64)
        if fingerprint_raw else ""
    )
    return ProjectCompanionSourceSnapshot(
        path_absolute=absolute,
        path_relative=relative,
        source_fingerprint=fingerprint,
        geometry_only=True,
        embedded_obj_asset_uuid=(
            _string(mapping["embedded_obj_asset_uuid"], "companion_source.embedded_obj_asset_uuid", 64, allow_empty=True)
            if int(schema_version) >= 8 else ""
        ),
        embedded_obj_schema_version=(
            _integer(mapping["embedded_obj_schema_version"], "companion_source.embedded_obj_schema_version", minimum=0)
            if int(schema_version) >= 8 else 0
        ),
        embedded_obj_data_name_hint=(
            _string(mapping["embedded_obj_data_name_hint"], "companion_source.embedded_obj_data_name_hint", PROJECT_PERSISTENCE_MAX_NAME_LENGTH, allow_empty=True)
            if int(schema_version) >= 8 else ""
        ),
        embedded_obj_geometry_revision=(
            _integer(mapping["embedded_obj_geometry_revision"], "companion_source.embedded_obj_geometry_revision", minimum=0)
            if int(schema_version) >= 8 else 0
        ),
    )


def _decode_axes(value: object, field_name: str, schema_version: int = PROJECT_PERSISTENCE_SCHEMA_VERSION) -> ProjectAxesSnapshot:
    """저장 버전별 축·이동·회전·양수 배율·중심과 최종 변환 지문을 검증합니다."""

    mapping = _mapping(value, field_name)
    required = {
        "preset_id", "axis_forward", "axis_up", "rotation_xyz_degrees",
        "display_transform_fingerprint",
    }
    if int(schema_version) >= 9:
        required.update({"translation_xyz", "scale_xyz", "pivot_source"})
    _require_keys(mapping, required, field_name)
    rotation = _optional_position(mapping["rotation_xyz_degrees"], f"{field_name}.rotation_xyz_degrees")
    if rotation is None:
        raise ProjectPersistenceCodecError(f"{field_name}.rotation_xyz_degrees는 세 회전값이어야 합니다.")
    extra = {}
    for key, default in (("translation_xyz", (0.0, 0.0, 0.0)), ("scale_xyz", (1.0, 1.0, 1.0)), ("pivot_source", (0.0, 0.0, 0.0))):
        vector = _optional_position(mapping[key], f"{field_name}.{key}") if int(schema_version) >= 9 else default
        if vector is None or (key == "scale_xyz" and any(value <= 0.0 for value in vector)):
            raise ProjectPersistenceCodecError(f"{field_name}.{key} 값이 올바르지 않습니다.")
        extra[key] = vector
    return ProjectAxesSnapshot(
        preset_id=_string(mapping["preset_id"], f"{field_name}.preset_id", 128),
        axis_forward=_string(mapping["axis_forward"], f"{field_name}.axis_forward", 8),
        axis_up=_string(mapping["axis_up"], f"{field_name}.axis_up", 8),
        rotation_xyz_degrees=rotation,
        display_transform_fingerprint=_hex_fingerprint(
            mapping["display_transform_fingerprint"], f"{field_name}.display_transform_fingerprint", 32,
        ),
        **extra,
    )


def _decode_model(value: object, schema_version: int) -> ProjectModelSnapshot:
    """지원 버전의 모델을 읽고 버전별 형상 축과 중심 변환을 검증합니다."""

    mapping = _mapping(value, "models 항목")
    v2 = "geometry_kind" in mapping
    required = {"model_id", "display_name", "load_order", "source", "catalog", "alignment"}
    if v2:
        required.add("geometry_kind")
    if int(schema_version) >= 4 and "companion_source" in mapping:
        required.add("companion_source")
    if int(schema_version) >= 5:
        required.add("current_axes")
        if "companion_source" in mapping:
            required.add("companion_axes")
    if int(schema_version) >= 8:
        required.update(
            {
                "embedded_obj_asset_uuid",
                "embedded_obj_schema_version",
                "embedded_obj_data_name_hint",
                "embedded_obj_geometry_revision",
            }
        )
    _require_keys(mapping, required, "models 항목")
    model_id = _string(mapping["model_id"], "model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
    display_name = _string(mapping["display_name"], "display_name", PROJECT_PERSISTENCE_MAX_NAME_LENGTH)
    catalog_mapping = _mapping(mapping["catalog"], "catalog")
    catalog_keys = {"registration_status", "is_anchor", "is_visible", "alignment_provenance"}
    if int(schema_version) >= 4:
        catalog_keys.update({"show_points", "show_surface"})
    _require_keys(catalog_mapping, catalog_keys, "catalog")
    status = _string(catalog_mapping["registration_status"], "catalog.registration_status", 64).upper()
    if status not in {MODEL_STATUS_PENDING, MODEL_STATUS_REGISTERED}:
        raise ProjectPersistenceCodecError("catalog.registration_status는 확정 PENDING 또는 REGISTERED여야 합니다.")
    source = _decode_source(mapping["source"], schema_version)
    if v2:
        geometry_kind = _string(mapping["geometry_kind"], "geometry_kind", 64).upper()
        if geometry_kind not in {GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH}:
            raise ProjectPersistenceCodecError("geometry_kind가 올바르지 않습니다.")
        if geometry_kind != source.geometry_kind:
            raise ProjectPersistenceCodecError("model.geometry_kind와 source.kind가 일치하지 않습니다.")
    return ProjectModelSnapshot(
        model_id=model_id,
        display_name=display_name,
        load_order=_integer(mapping["load_order"], "load_order", minimum=1),
        source=source,
        catalog=ProjectCatalogSnapshot(
            registration_status=status,
            is_anchor=_boolean(catalog_mapping["is_anchor"], "catalog.is_anchor"),
            is_visible=_boolean(catalog_mapping["is_visible"], "catalog.is_visible"),
            alignment_provenance=_decode_provenance(catalog_mapping["alignment_provenance"], model_id, schema_version),
            show_points=_boolean(catalog_mapping["show_points"], "catalog.show_points") if int(schema_version) >= 4 else True,
            show_surface=_boolean(catalog_mapping["show_surface"], "catalog.show_surface") if int(schema_version) >= 4 else True,
        ),
        alignment_matrix_float64=_decode_alignment(mapping["alignment"]),
        companion_source=(
            _decode_companion_source(mapping["companion_source"], schema_version)
            if "companion_source" in mapping else None
        ),
        current_axes=_decode_axes(mapping["current_axes"], "current_axes", schema_version) if int(schema_version) >= 5 else None,
        companion_axes=_decode_axes(mapping["companion_axes"], "companion_axes", schema_version) if "companion_axes" in mapping else None,
        embedded_obj_asset_uuid=(
            _string(mapping["embedded_obj_asset_uuid"], "embedded_obj_asset_uuid", 64, allow_empty=True)
            if int(schema_version) >= 8 else ""
        ),
        embedded_obj_schema_version=(
            _integer(mapping["embedded_obj_schema_version"], "embedded_obj_schema_version", minimum=0)
            if int(schema_version) >= 8 else 0
        ),
        embedded_obj_data_name_hint=(
            _string(mapping["embedded_obj_data_name_hint"], "embedded_obj_data_name_hint", PROJECT_PERSISTENCE_MAX_NAME_LENGTH, allow_empty=True)
            if int(schema_version) >= 8 else ""
        ),
        embedded_obj_geometry_revision=(
            _integer(mapping["embedded_obj_geometry_revision"], "embedded_obj_geometry_revision", minimum=0)
            if int(schema_version) >= 8 else 0
        ),
    )


def _decode_marker_point(
    value: object, field_name: str, expected_role: str, schema_version: int,
) -> ProjectMarkerPointSnapshot:
    """기존 점·면 참조와 v6 형상 출처를 검증하고 과거 마커는 주형상으로 복원합니다."""

    mapping = _mapping(value, field_name)
    v3 = "local_position" in mapping
    v2 = "point_kind" in mapping or "source_point_id" in mapping
    _require_keys(
        mapping,
        (
            {"model_role", "model_id", "point_kind", "source_point_id", "local_position", "geometry_source"}
            if schema_version >= 6
            else {"model_role", "model_id", "point_kind", "source_point_id", "local_position"}
            if v3
            else {"model_role", "model_id", "point_kind", "source_point_id"}
            if v2
            else {"model_role", "model_id", "point3d_id"}
        ),
        field_name,
    )
    role = _string(mapping["model_role"], f"{field_name}.model_role", 32).upper()
    if role != expected_role:
        raise ProjectPersistenceCodecError(f"{field_name}.model_role이 {expected_role}가 아닙니다.")
    geometry_source = (
        _string(mapping["geometry_source"], f"{field_name}.geometry_source", 32).upper()
        if schema_version >= 6 else MARKER_GEOMETRY_SOURCE_MAIN
    )
    if geometry_source not in MARKER_GEOMETRY_SOURCES:
        raise ProjectPersistenceCodecError(f"{field_name}.geometry_source가 올바르지 않습니다.")
    point_kind = MARKER_POINT_KIND_COLMAP
    source_id = mapping["source_point_id"] if v2 else mapping["point3d_id"]
    if v2:
        point_kind = _string(mapping["point_kind"], f"{field_name}.point_kind", 32).upper()
        if point_kind not in MARKER_POINT_KINDS:
            raise ProjectPersistenceCodecError(f"{field_name}.point_kind가 올바르지 않습니다.")
    local_position = (
        _optional_position(mapping["local_position"], f"{field_name}.local_position")
        if v3
        else None
    )
    if point_kind == MARKER_POINT_KIND_MESH_SURFACE and local_position is None:
        raise ProjectPersistenceCodecError(f"{field_name} 면 마커의 local_position이 비어 있습니다.")
    return ProjectMarkerPointSnapshot(
        model_role=role,
        model_id=_string(mapping["model_id"], f"{field_name}.model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH),
        point3d_id=_integer(source_id, f"{field_name}.source_point_id", minimum=0),
        point_kind=point_kind,
        source_point_id=_integer(source_id, f"{field_name}.source_point_id", minimum=0),
        local_position=local_position,
        geometry_source=geometry_source,
    )


def _decode_markers(value: object, schema_version: int) -> ProjectMarkerSnapshot:
    """입력 중 상태를 제외한 완료 마커 목록을 저장 버전에 맞춰 복원합니다."""

    mapping = _mapping(value, "markers")
    _require_keys(mapping, {"next_pair_id", "active_pair_id", "completed_pairs"}, "markers")
    pairs_value = _list(mapping["completed_pairs"], "markers.completed_pairs")
    if len(pairs_value) > PROJECT_PERSISTENCE_MAX_MARKERS:
        raise ProjectPersistenceCodecError("완료 마커 수가 10,000쌍 제한을 초과했습니다.")
    pairs: list[ProjectMarkerPairSnapshot] = []
    pair_ids: set[int] = set()
    for index, raw_pair in enumerate(pairs_value):
        pair_mapping = _mapping(raw_pair, f"markers.completed_pairs[{index}]")
        _require_keys(pair_mapping, {"pair_id", "reference", "moving"}, f"markers.completed_pairs[{index}]")
        pair_id = _integer(pair_mapping["pair_id"], "marker.pair_id", minimum=1)
        if pair_id in pair_ids:
            raise ProjectPersistenceCodecError("marker.pair_id가 중복되었습니다.")
        pair_ids.add(pair_id)
        pairs.append(
            ProjectMarkerPairSnapshot(
                pair_id=pair_id,
                reference=_decode_marker_point(pair_mapping["reference"], "marker.reference", ROLE_REFERENCE, schema_version),
                moving=_decode_marker_point(pair_mapping["moving"], "marker.moving", ROLE_MOVING, schema_version),
            )
        )
    active_value = mapping["active_pair_id"]
    active_pair_id = None if active_value is None else _integer(active_value, "markers.active_pair_id", minimum=1)
    if active_pair_id is not None and active_pair_id not in pair_ids:
        raise ProjectPersistenceCodecError("markers.active_pair_id가 완료 마커 목록에 없습니다.")
    next_pair_id = _integer(mapping["next_pair_id"], "markers.next_pair_id", minimum=1)
    if pair_ids and next_pair_id <= max(pair_ids):
        raise ProjectPersistenceCodecError("markers.next_pair_id는 완료 marker ID보다 커야 합니다.")
    return ProjectMarkerSnapshot(
        next_pair_id=next_pair_id,
        active_pair_id=active_pair_id,
        completed_pairs=tuple(pairs),
    )


def _validate_source_snapshot(source: ProjectSourceSnapshot) -> None:
    """직접 생성한 source snapshot도 decode 경로와 같은 저장 경계를 통과시킵니다."""

    if not isinstance(source, ProjectSourceSnapshot):
        raise ProjectPersistenceCodecError("source snapshot 형식이 올바르지 않습니다.")
    _string(
        source.selected_root_absolute,
        "source.selected_root_absolute",
        PROJECT_PERSISTENCE_MAX_PATH_LENGTH,
    )
    _string(source.selected_root_relative, "source.selected_root_relative", PROJECT_PERSISTENCE_MAX_PATH_LENGTH, allow_empty=True)
    _string(
        source.sparse_path_absolute,
        "source.sparse_path_absolute",
        PROJECT_PERSISTENCE_MAX_PATH_LENGTH,
    )
    _string(
        source.sparse_path_relative,
        "source.sparse_path_relative",
        PROJECT_PERSISTENCE_MAX_PATH_LENGTH,
        allow_empty=True,
    )
    if str(source.source_format).strip().lower() not in {"binary", "text", "obj"}:
        raise ProjectPersistenceCodecError("source.source_format은 binary, text 또는 obj여야 합니다.")
    if str(source.geometry_kind).strip().upper() not in {GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH}:
        raise ProjectPersistenceCodecError("source.geometry_kind가 올바르지 않습니다.")
    if str(source.kind).strip().upper() not in {"COLMAP", "OBJ"}:
        raise ProjectPersistenceCodecError("source.kind가 올바르지 않습니다.")
    kind = str(source.kind).strip().upper()
    source_format = str(source.source_format).strip().lower()
    if (kind == "OBJ" and source_format != "obj") or (kind == "COLMAP" and source_format == "obj"):
        raise ProjectPersistenceCodecError("source.kind와 source.source_format 조합이 일치하지 않습니다.")
    expected_geometry = GEOMETRY_KIND_TRIANGLE_MESH if kind == "OBJ" else GEOMETRY_KIND_COLMAP_POINT_CLOUD
    if str(source.geometry_kind).strip().upper() != expected_geometry:
        raise ProjectPersistenceCodecError("source.geometry_kind와 source.kind가 일치하지 않습니다.")
    if source.source_fingerprint:
        _hex_fingerprint(source.source_fingerprint, "source.source_fingerprint", 64)
    elif str(source.kind).strip().upper() != "OBJ":
        raise ProjectPersistenceCodecError("COLMAP source fingerprint가 비어 있습니다.")
    _integer(source.display_point_limit, "source.display_point_limit", minimum=1)
    _integer(source.display_vertex_limit, "source.display_vertex_limit", minimum=1)
    _string(source.axis_preset, "source.axis_preset", 128)
    _string(source.axis_forward, "source.axis_forward", 8)
    _string(source.axis_up, "source.axis_up", 8)
    _hex_fingerprint(
        source.display_transform_fingerprint,
        "source.display_transform_fingerprint",
        32,
    )


def _validate_marker_point_snapshot(
    point: ProjectMarkerPointSnapshot,
    *,
    expected_role: str,
    field_name: str,
) -> None:
    """직접 생성한 마커의 역할·원본 참조·형상 출처 범위를 엄격히 검증합니다."""

    if not isinstance(point, ProjectMarkerPointSnapshot):
        raise ProjectPersistenceCodecError(f"{field_name} snapshot 형식이 올바르지 않습니다.")
    if str(point.model_role).strip().upper() != expected_role:
        raise ProjectPersistenceCodecError(f"{field_name}.model_role이 {expected_role}가 아닙니다.")
    _string(point.model_id, f"{field_name}.model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
    _integer(point.source_point_id, f"{field_name}.source_point_id", minimum=0)
    if str(point.point_kind).strip().upper() not in MARKER_POINT_KINDS:
        raise ProjectPersistenceCodecError(f"{field_name}.point_kind가 올바르지 않습니다.")
    if point.geometry_source not in MARKER_GEOMETRY_SOURCES:
        raise ProjectPersistenceCodecError(f"{field_name}.geometry_source가 올바르지 않습니다.")
    if point.geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION and point.point_kind != MARKER_POINT_KIND_MESH_SURFACE:
        raise ProjectPersistenceCodecError("연결 메시 마커는 삼각면 위치만 참조할 수 있습니다.")
    local_position = _optional_position(point.local_position, f"{field_name}.local_position")
    if point.point_kind == MARKER_POINT_KIND_MESH_SURFACE and local_position is None:
        raise ProjectPersistenceCodecError(f"{field_name} 면 마커의 local_position이 비어 있습니다.")


def _validate_marker_snapshot(snapshot: ProjectMarkerSnapshot) -> None:
    """직접 생성한 완료 marker 목록의 ID·선택·endpoint 불변조건을 검증합니다."""

    if not isinstance(snapshot, ProjectMarkerSnapshot):
        raise ProjectPersistenceCodecError("markers snapshot 형식이 올바르지 않습니다.")
    if not isinstance(snapshot.completed_pairs, tuple):
        raise ProjectPersistenceCodecError("markers.completed_pairs는 불변 tuple이어야 합니다.")
    if len(snapshot.completed_pairs) > PROJECT_PERSISTENCE_MAX_MARKERS:
        raise ProjectPersistenceCodecError("완료 마커 수가 10,000쌍 제한을 초과했습니다.")
    pair_ids: set[int] = set()
    for pair in snapshot.completed_pairs:
        if not isinstance(pair, ProjectMarkerPairSnapshot):
            raise ProjectPersistenceCodecError("marker pair snapshot 형식이 올바르지 않습니다.")
        pair_id = _integer(pair.pair_id, "marker.pair_id", minimum=1)
        if pair_id in pair_ids:
            raise ProjectPersistenceCodecError("marker.pair_id가 중복되었습니다.")
        pair_ids.add(pair_id)
        _validate_marker_point_snapshot(
            pair.reference,
            expected_role=ROLE_REFERENCE,
            field_name="marker.reference",
        )
        _validate_marker_point_snapshot(
            pair.moving,
            expected_role=ROLE_MOVING,
            field_name="marker.moving",
        )
    active_pair_id = snapshot.active_pair_id
    if active_pair_id is not None:
        active_pair_id = _integer(active_pair_id, "markers.active_pair_id", minimum=1)
        if active_pair_id not in pair_ids:
            raise ProjectPersistenceCodecError("markers.active_pair_id가 완료 마커 목록에 없습니다.")
    next_pair_id = _integer(snapshot.next_pair_id, "markers.next_pair_id", minimum=1)
    if pair_ids and next_pair_id <= max(pair_ids):
        raise ProjectPersistenceCodecError("markers.next_pair_id는 완료 marker ID보다 커야 합니다.")


def _validate_snapshot_semantics(snapshot: ProjectStateSnapshot) -> None:
    """저장 형식·형상 참조·단일 표시를 검증하고 역할별 과거 마커 충돌은 보존합니다."""

    if not isinstance(snapshot, ProjectStateSnapshot):
        raise ProjectPersistenceCodecError("프로젝트 상태 snapshot 형식이 올바르지 않습니다.")
    schema_version = _integer(snapshot.schema_version, "schema_version", minimum=1)
    if schema_version not in PROJECT_PERSISTENCE_SUPPORTED_SCHEMA_VERSIONS:
        raise ProjectPersistenceCodecError("지원하지 않는 프로젝트 상태 schema 버전입니다.")
    if not isinstance(snapshot.models, tuple):
        raise ProjectPersistenceCodecError("models는 불변 tuple이어야 합니다.")
    if len(snapshot.models) > PROJECT_PERSISTENCE_MAX_MODELS:
        raise ProjectPersistenceCodecError("모델 수가 256개 제한을 초과했습니다.")
    if not isinstance(snapshot.writer_addon_version, tuple) or len(snapshot.writer_addon_version) != 3 or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in snapshot.writer_addon_version
    ):
        raise ProjectPersistenceCodecError("writer_addon_version은 음수가 아닌 세 정수여야 합니다.")
    project_uuid = _string(snapshot.project_uuid, "project_uuid", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
    try:
        UUID(project_uuid)
    except (ValueError, AttributeError, TypeError) as error:
        raise ProjectPersistenceCodecError("project_uuid 형식이 올바르지 않습니다.") from error
    saved_at_utc = _string(snapshot.saved_at_utc, "saved_at_utc", 64)
    if not saved_at_utc.endswith("Z"):
        raise ProjectPersistenceCodecError("saved_at_utc는 UTC Z 표기여야 합니다.")
    try:
        datetime.fromisoformat(saved_at_utc.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ProjectPersistenceCodecError("saved_at_utc가 ISO-8601 형식이 아닙니다.") from error
    _validate_marker_snapshot(snapshot.markers)
    if schema_version < 3 and any(
        point.point_kind == MARKER_POINT_KIND_MESH_SURFACE
        for pair in snapshot.markers.completed_pairs
        for point in (pair.reference, pair.moving)
    ):
        raise ProjectPersistenceCodecError("메시 면 마커는 프로젝트 schema v3에서만 저장할 수 있습니다.")
    for model in snapshot.models:
        if not isinstance(model, ProjectModelSnapshot):
            raise ProjectPersistenceCodecError("model snapshot 형식이 올바르지 않습니다.")
        if not isinstance(model.catalog, ProjectCatalogSnapshot):
            raise ProjectPersistenceCodecError("catalog snapshot 형식이 올바르지 않습니다.")
    model_ids = [model.model_id for model in snapshot.models]
    if len(model_ids) != len(set(model_ids)):
        raise ProjectPersistenceCodecError("model_id가 중복되었습니다.")
    load_orders = [model.load_order for model in snapshot.models]
    if len(load_orders) != len(set(load_orders)):
        raise ProjectPersistenceCodecError("load_order가 중복되었습니다.")
    if snapshot.models:
        anchor_count = sum(
            1
            for model in snapshot.models
            if model.catalog.is_anchor and model.catalog.registration_status == MODEL_STATUS_REGISTERED
        )
        if anchor_count != 1:
            raise ProjectPersistenceCodecError("A 그룹에는 Anchor가 정확히 하나여야 합니다.")
    for model in snapshot.models:
        _string(model.model_id, "model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
        _string(model.display_name, "display_name", PROJECT_PERSISTENCE_MAX_NAME_LENGTH)
        _integer(model.load_order, "load_order", minimum=1)
        if model.catalog.registration_status not in {MODEL_STATUS_PENDING, MODEL_STATUS_REGISTERED}:
            raise ProjectPersistenceCodecError("catalog.registration_status는 확정 상태여야 합니다.")
        if not isinstance(model.catalog.is_anchor, bool) or not isinstance(model.catalog.is_visible, bool):
            raise ProjectPersistenceCodecError("catalog의 anchor·visibility는 bool이어야 합니다.")
        if model.catalog.is_anchor and model.catalog.registration_status != MODEL_STATUS_REGISTERED:
            raise ProjectPersistenceCodecError("Anchor는 REGISTERED 모델이어야 합니다.")
        provenance = model.catalog.alignment_provenance
        if provenance is not None:
            if not isinstance(provenance, AlignmentProvenanceRecord):
                raise ProjectPersistenceCodecError("alignment_provenance 형식이 올바르지 않습니다.")
            if provenance.model_id != model.model_id:
                raise ProjectPersistenceCodecError("alignment_provenance 모델 ID가 상위 모델과 다릅니다.")
        _validate_source_snapshot(model.source)
        embedded_values = (
            str(model.embedded_obj_asset_uuid).strip(),
            int(model.embedded_obj_schema_version),
            str(model.embedded_obj_data_name_hint).strip(),
            int(model.embedded_obj_geometry_revision),
        )
        if embedded_values[0]:
            try:
                UUID(embedded_values[0])
            except (ValueError, AttributeError, TypeError) as error:
                raise ProjectPersistenceCodecError("내장 OBJ asset UUID가 올바르지 않습니다.") from error
            if embedded_values[1] < 1 or not embedded_values[2] or embedded_values[3] < 1:
                raise ProjectPersistenceCodecError("내장 OBJ schema·이름 힌트·revision이 올바르지 않습니다.")
        elif any(embedded_values[1:]):
            raise ProjectPersistenceCodecError("내장 OBJ UUID 없이 부속 식별자를 저장할 수 없습니다.")
        if schema_version >= 8 and model.source.geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH:
            if not embedded_values[0]:
                raise ProjectPersistenceCodecError("v8 OBJ 모델에는 내장 Mesh 식별자가 필요합니다.")
            if model.source.source_fingerprint:
                raise ProjectPersistenceCodecError("v8 OBJ 모델은 외부 source fingerprint를 저장할 수 없습니다.")
        for field_name, axes in (("current_axes", model.current_axes), ("companion_axes", model.companion_axes)):
            required_axes = field_name == "current_axes" or model.companion_source is not None
            if schema_version >= 5 and required_axes and axes is None:
                raise ProjectPersistenceCodecError(f"{field_name}에 확정 표시 축이 필요합니다.")
            if axes is not None:
                if schema_version < 5:
                    raise ProjectPersistenceCodecError("형상별 현재 축은 프로젝트 schema v5 이상에서만 저장할 수 있습니다.")
                if not isinstance(axes, ProjectAxesSnapshot):
                    raise ProjectPersistenceCodecError(f"{field_name} snapshot 형식이 올바르지 않습니다.")
                _decode_axes(_axes_mapping(axes, schema_version), field_name, schema_version)
        if model.companion_axes is not None and model.companion_source is None:
            raise ProjectPersistenceCodecError("연결 OBJ 출처 없이 연결 메시 축을 저장할 수 없습니다.")
        _boolean(model.catalog.show_points, "catalog.show_points")
        _boolean(model.catalog.show_surface, "catalog.show_surface")
        if model.companion_source is not None:
            if schema_version < 4:
                raise ProjectPersistenceCodecError("연결 OBJ는 프로젝트 schema v4 이상에서만 저장할 수 있습니다.")
            if model.source.geometry_kind != GEOMETRY_KIND_COLMAP_POINT_CLOUD:
                raise ProjectPersistenceCodecError("연결 OBJ의 소유 모델은 COLMAP이어야 합니다.")
            if not isinstance(model.companion_source, ProjectCompanionSourceSnapshot):
                raise ProjectPersistenceCodecError("연결 OBJ 출처 snapshot 형식이 올바르지 않습니다.")
            _decode_companion_source(
                _companion_source_mapping(model.companion_source, schema_version),
                schema_version,
            )
            companion_values = (
                str(model.companion_source.embedded_obj_asset_uuid).strip(),
                int(model.companion_source.embedded_obj_schema_version),
                str(model.companion_source.embedded_obj_data_name_hint).strip(),
                int(model.companion_source.embedded_obj_geometry_revision),
            )
            if companion_values[0]:
                try:
                    UUID(companion_values[0])
                except (ValueError, AttributeError, TypeError) as error:
                    raise ProjectPersistenceCodecError("연결 내장 OBJ asset UUID가 올바르지 않습니다.") from error
                if companion_values[1] < 1 or not companion_values[2] or companion_values[3] < 1:
                    raise ProjectPersistenceCodecError("연결 내장 OBJ 식별자가 올바르지 않습니다.")
            elif any(companion_values[1:]):
                raise ProjectPersistenceCodecError("연결 내장 OBJ UUID 없이 부속 식별자를 저장할 수 없습니다.")
            if schema_version >= 8:
                if not companion_values[0]:
                    raise ProjectPersistenceCodecError("v8 연결 OBJ에는 내장 Mesh 식별자가 필요합니다.")
                if model.companion_source.source_fingerprint:
                    raise ProjectPersistenceCodecError("v8 연결 OBJ는 외부 source fingerprint를 저장할 수 없습니다.")
        if schema_version >= 6:
            if model.catalog.show_points == model.catalog.show_surface:
                raise ProjectPersistenceCodecError("레이어는 점군과 메시 중 한 형상을 선택해야 합니다.")
            if model.source.geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH and model.catalog.show_points:
                raise ProjectPersistenceCodecError("독립 OBJ 레이어는 메시를 선택해야 합니다.")
            if model.source.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD and model.companion_source is None and model.catalog.show_surface:
                raise ProjectPersistenceCodecError("연결 메시가 없는 COLMAP 레이어는 점군을 선택해야 합니다.")
        try:
            Sim3Transform(matrix_float64=model.alignment_matrix_float64)
        except (TypeError, ValueError) as error:
            raise ProjectPersistenceCodecError(f"alignment Sim(3)이 올바르지 않습니다: {error}") from error
    model_by_id = {model.model_id: model for model in snapshot.models}
    for model in snapshot.models:
        provenance = model.catalog.alignment_provenance
        if provenance is None:
            continue
        if (provenance.solver_geometry_dependencies or provenance.engine_metadata_json) and schema_version < 7:
            raise ProjectPersistenceCodecError("CC 정밀 형상과 엔진 출처는 프로젝트 v7 이상에서만 저장할 수 있습니다.")
        dependencies = provenance.geometry_dependencies
        if dependencies and schema_version < 6:
            raise ProjectPersistenceCodecError("마커 형상 의존성은 프로젝트 v6 이상에서만 저장할 수 있습니다.")
        if len(dependencies) > PROJECT_PERSISTENCE_MAX_MODELS * 2:
            raise ProjectPersistenceCodecError("마커 형상 의존성 수가 모델 형상 수 제한을 초과했습니다.")
        if len(dependencies) != len(set(dependencies)):
            raise ProjectPersistenceCodecError("마커 형상 의존성이 중복되었습니다.")
        for owner_id, source in dependencies:
            _string(owner_id, "마커 의존 모델 ID", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
            if source not in MARKER_GEOMETRY_SOURCES:
                raise ProjectPersistenceCodecError("마커 의존 형상 출처가 올바르지 않습니다.")
    used_points: set[tuple[object, ...]] = set()
    for pair in snapshot.markers.completed_pairs:
        for point, expected_status in (
            (pair.reference, MODEL_STATUS_REGISTERED),
            (pair.moving, MODEL_STATUS_PENDING),
        ):
            model = model_by_id.get(point.model_id)
            if model is None:
                raise ProjectPersistenceCodecError("marker가 존재하지 않는 model_id를 참조합니다.")
            if model.catalog.registration_status != expected_status:
                raise ProjectPersistenceCodecError("marker 역할과 Data Layers 그룹 상태가 일치하지 않습니다.")
            if point.geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION:
                if schema_version < 6:
                    raise ProjectPersistenceCodecError("연결 메시 마커는 프로젝트 v6 이상에서만 저장할 수 있습니다.")
                if model.companion_source is None:
                    raise ProjectPersistenceCodecError("연결 메시 마커가 연결 OBJ 출처 없는 모델을 참조합니다.")
            elif model.source.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD:
                if point.point_kind != MARKER_POINT_KIND_COLMAP:
                    raise ProjectPersistenceCodecError("COLMAP 주형상 마커는 원본 점을 참조해야 합니다.")
            elif point.point_kind not in {MARKER_POINT_KIND_MESH_VERTEX, MARKER_POINT_KIND_MESH_SURFACE}:
                raise ProjectPersistenceCodecError("독립 OBJ 마커는 원본 정점 또는 삼각면을 참조해야 합니다.")
            key = (
                point.model_role,
                point.model_id,
                point.geometry_source,
                point.point_kind,
                point.source_point_id,
                point.local_position if point.point_kind == MARKER_POINT_KIND_MESH_SURFACE else None,
            )
            if key in used_points:
                raise ProjectPersistenceCodecError("같은 원본 점을 여러 완료 marker에 사용할 수 없습니다.")
            used_points.add(key)


def decode_project_state(payload: str | bytes) -> ProjectStateSnapshot:
    """v1~v8 원문을 검증하고 기존 정렬을 보존한 현재 스냅샷으로 반환합니다."""

    mapping = _decode_json(payload)
    expected = {
        "schema_version",
        "writer_addon_version",
        "project_uuid",
        "saved_at_utc",
        "models",
        "markers",
        "payload_fingerprint",
    }
    _require_keys(mapping, expected, "프로젝트 상태")
    schema_version = _integer(mapping["schema_version"], "schema_version", minimum=1)
    if schema_version not in PROJECT_PERSISTENCE_SUPPORTED_SCHEMA_VERSIONS:
        raise ProjectPersistenceCodecError(f"지원하지 않는 프로젝트 상태 schema 버전입니다: {schema_version}")
    version_values = _list(mapping["writer_addon_version"], "writer_addon_version")
    if len(version_values) != 3:
        raise ProjectPersistenceCodecError("writer_addon_version은 세 항목이어야 합니다.")
    models_value = _list(mapping["models"], "models")
    if len(models_value) > PROJECT_PERSISTENCE_MAX_MODELS:
        raise ProjectPersistenceCodecError("모델 수가 256개 제한을 초과했습니다.")
    snapshot = ProjectStateSnapshot(
        schema_version=schema_version,
        writer_addon_version=tuple(_integer(value, "writer_addon_version", minimum=0) for value in version_values),
        project_uuid=_string(mapping["project_uuid"], "project_uuid", PROJECT_PERSISTENCE_MAX_ID_LENGTH),
        saved_at_utc=_string(mapping["saved_at_utc"], "saved_at_utc", 64),
        models=tuple(_decode_model(value, schema_version) for value in models_value),
        markers=_decode_markers(mapping["markers"], schema_version),
        payload_fingerprint=_string(mapping["payload_fingerprint"], "payload_fingerprint", 128),
    )
    raw_without_fingerprint = dict(mapping)
    raw_without_fingerprint.pop("payload_fingerprint", None)
    expected_fingerprint = hashlib.sha256(_canonical_json_bytes(raw_without_fingerprint)).hexdigest()
    if snapshot.payload_fingerprint != expected_fingerprint:
        raise ProjectPersistenceCodecError("프로젝트 상태 payload fingerprint가 일치하지 않습니다.")
    _validate_snapshot_semantics(snapshot)
    return _upgrade_snapshot(snapshot)


__all__ = (
    "ProjectPersistenceCodecError",
    "decode_project_state",
    "encode_project_state",
    "payload_fingerprint_for_snapshot",
    "project_state_schema_version",
)
