"""버전화된 프로젝트 상태를 엄격한 canonical JSON으로 변환합니다."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from math import isfinite
from typing import Any
from uuid import UUID

from ..alignment.types import AlignmentProvenanceRecord, Sim3Transform
from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
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
    ProjectCatalogSnapshot,
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


def _provenance_mapping(value: AlignmentProvenanceRecord | None) -> dict[str, object] | None:
    """불변 provenance를 JSON의 원시 매핑으로 안전하게 변환합니다."""

    return None if value is None else value.to_mapping()


def _source_mapping(source: ProjectSourceSnapshot) -> dict[str, object]:
    """원본 snapshot을 schema v2 source 객체로 변환합니다."""

    path_absolute = source.path_absolute
    path_relative = source.path_relative
    if source.geometry_kind == GEOMETRY_KIND_COLMAP_POINT_CLOUD:
        # v1 호환 객체가 sparse 별칭만 수정한 경우에도 저장된 변경을 보존합니다.
        path_absolute = source.sparse_path_absolute or path_absolute
        path_relative = source.sparse_path_relative
    return {
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


def _model_mapping(model: ProjectModelSnapshot) -> dict[str, object]:
    """모델 snapshot을 schema v2 JSON 객체로 변환합니다."""

    return {
        "model_id": model.model_id,
        "display_name": model.display_name,
        "load_order": model.load_order,
        "geometry_kind": model.source.geometry_kind,
        "source": _source_mapping(model.source),
        "catalog": {
            "registration_status": model.catalog.registration_status,
            "is_anchor": model.catalog.is_anchor,
            "is_visible": model.catalog.is_visible,
            "alignment_provenance": _provenance_mapping(model.catalog.alignment_provenance),
        },
        "alignment": {
            "matrix_float64": [list(row) for row in model.alignment_matrix_float64],
        },
    }


def _marker_point_mapping(
    point: ProjectMarkerPointSnapshot,
    schema_version: int,
) -> dict[str, object]:
    """마커 한쪽의 원본 참조와 v3 선택 좌표를 schema 객체로 변환합니다."""

    mapping: dict[str, object] = {
        "model_role": point.model_role,
        "model_id": point.model_id,
        "point_kind": point.point_kind,
        "source_point_id": point.source_point_id,
    }
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
        "models": [_model_mapping(model) for model in snapshot.models],
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
    """호환 snapshot을 현재 schema v3 JSON으로 올리고 payload 지문을 포함합니다."""

    _validate_snapshot_semantics(snapshot)
    if int(snapshot.schema_version) != PROJECT_PERSISTENCE_SCHEMA_VERSION:
        snapshot = replace(snapshot, schema_version=PROJECT_PERSISTENCE_SCHEMA_VERSION)
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


def _decode_provenance(value: object, model_id: str) -> AlignmentProvenanceRecord | None:
    """선택 provenance 객체를 기존 불변 감사 타입으로 복원합니다."""

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
        )
    except ValueError as error:
        raise ProjectPersistenceCodecError(f"alignment_provenance가 올바르지 않습니다: {error}") from error


def _decode_source(value: object) -> ProjectSourceSnapshot:
    """schema v1 COLMAP 또는 schema v2 COLMAP·OBJ source를 검증해 복원합니다."""

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
    fingerprint = _hex_fingerprint(
        mapping["source_fingerprint"],
        "source.source_fingerprint",
        64,
    )
    transform_fingerprint = _hex_fingerprint(
        mapping["display_transform_fingerprint"],
        "source.display_transform_fingerprint",
        32,
    )
    return ProjectSourceSnapshot(
        selected_root_absolute=(
            _string(mapping["selected_root_absolute"], "source.selected_root_absolute", PROJECT_PERSISTENCE_MAX_PATH_LENGTH)
            if not v2
            else path_absolute
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


def _decode_model(value: object) -> ProjectModelSnapshot:
    """schema v1·v2 모델 객체를 source·catalog·alignment 구성 요소로 복원합니다."""

    mapping = _mapping(value, "models 항목")
    v2 = "geometry_kind" in mapping
    required = {"model_id", "display_name", "load_order", "source", "catalog", "alignment"}
    if v2:
        required.add("geometry_kind")
    _require_keys(mapping, required, "models 항목")
    model_id = _string(mapping["model_id"], "model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
    display_name = _string(mapping["display_name"], "display_name", PROJECT_PERSISTENCE_MAX_NAME_LENGTH)
    catalog_mapping = _mapping(mapping["catalog"], "catalog")
    _require_keys(catalog_mapping, {"registration_status", "is_anchor", "is_visible", "alignment_provenance"}, "catalog")
    status = _string(catalog_mapping["registration_status"], "catalog.registration_status", 64).upper()
    if status not in {MODEL_STATUS_PENDING, MODEL_STATUS_REGISTERED}:
        raise ProjectPersistenceCodecError("catalog.registration_status는 확정 PENDING 또는 REGISTERED여야 합니다.")
    source = _decode_source(mapping["source"])
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
            alignment_provenance=_decode_provenance(catalog_mapping["alignment_provenance"], model_id),
        ),
        alignment_matrix_float64=_decode_alignment(mapping["alignment"]),
    )


def _decode_marker_point(value: object, field_name: str, expected_role: str) -> ProjectMarkerPointSnapshot:
    """schema v1 POINT3D, v2 등록점, v3 면 선택 좌표를 검증해 복원합니다."""

    mapping = _mapping(value, field_name)
    v3 = "local_position" in mapping
    v2 = "point_kind" in mapping or "source_point_id" in mapping
    _require_keys(
        mapping,
        (
            {"model_role", "model_id", "point_kind", "source_point_id", "local_position"}
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
    )


def _decode_markers(value: object) -> ProjectMarkerSnapshot:
    """드래프트 없는 완료 마커 목록을 schema v1 값으로 복원합니다."""

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
                reference=_decode_marker_point(pair_mapping["reference"], "marker.reference", ROLE_REFERENCE),
                moving=_decode_marker_point(pair_mapping["moving"], "marker.moving", ROLE_MOVING),
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
    _hex_fingerprint(source.source_fingerprint, "source.source_fingerprint", 64)
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
    """직접 생성한 marker endpoint의 역할·참조 범위를 엄격히 검증합니다."""

    if not isinstance(point, ProjectMarkerPointSnapshot):
        raise ProjectPersistenceCodecError(f"{field_name} snapshot 형식이 올바르지 않습니다.")
    if str(point.model_role).strip().upper() != expected_role:
        raise ProjectPersistenceCodecError(f"{field_name}.model_role이 {expected_role}가 아닙니다.")
    _string(point.model_id, f"{field_name}.model_id", PROJECT_PERSISTENCE_MAX_ID_LENGTH)
    _integer(point.source_point_id, f"{field_name}.source_point_id", minimum=0)
    if str(point.point_kind).strip().upper() not in MARKER_POINT_KINDS:
        raise ProjectPersistenceCodecError(f"{field_name}.point_kind가 올바르지 않습니다.")
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
    """생성·복원 공통으로 schema 값·모델 참조·marker 구조를 검증합니다."""

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
        try:
            Sim3Transform(matrix_float64=model.alignment_matrix_float64)
        except (TypeError, ValueError) as error:
            raise ProjectPersistenceCodecError(f"alignment Sim(3)이 올바르지 않습니다: {error}") from error
    model_by_id = {model.model_id: model for model in snapshot.models}
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
            key = (
                point.model_role,
                point.model_id,
                point.point_kind,
                point.source_point_id,
                point.local_position if point.point_kind == MARKER_POINT_KIND_MESH_SURFACE else None,
            )
            if key in used_points:
                raise ProjectPersistenceCodecError("같은 원본 점을 여러 완료 marker에 사용할 수 없습니다.")
            used_points.add(key)


def decode_project_state(payload: str | bytes) -> ProjectStateSnapshot:
    """Text 원문을 schema v1·v2·v3으로 읽고 검증한 뒤 현재 snapshot으로 반환합니다."""

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
        models=tuple(_decode_model(value) for value in models_value),
        markers=_decode_markers(mapping["markers"]),
        payload_fingerprint=_string(mapping["payload_fingerprint"], "payload_fingerprint", 128),
    )
    if schema_version == 1:
        raw_without_fingerprint = dict(mapping)
        raw_without_fingerprint.pop("payload_fingerprint", None)
        expected_fingerprint = hashlib.sha256(_canonical_json_bytes(raw_without_fingerprint)).hexdigest()
    else:
        expected_fingerprint = payload_fingerprint_for_snapshot(snapshot)
    if snapshot.payload_fingerprint != expected_fingerprint:
        raise ProjectPersistenceCodecError("프로젝트 상태 payload fingerprint가 일치하지 않습니다.")
    _validate_snapshot_semantics(snapshot)
    return replace(snapshot, schema_version=PROJECT_PERSISTENCE_SCHEMA_VERSION)


__all__ = (
    "ProjectPersistenceCodecError",
    "decode_project_state",
    "encode_project_state",
    "payload_fingerprint_for_snapshot",
    "project_state_schema_version",
)
