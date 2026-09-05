"""메시 삼각면 피킹·마커 검증·프로젝트 저장 계약을 함께 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 경로를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _mesh_model(model_id: str = "mesh"):
    """면 피킹에 사용할 두 삼각면 정사각형 메시를 생성합니다."""

    from tools.align.mesh_io.types import TriangleMeshModelData

    vertices = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        dtype=np.float64,
    )
    return TriangleMeshModelData(
        model_id=model_id,
        display_name=model_id,
        source_path=Path.cwd() / f"{model_id}.obj",
        vertices_float64=vertices,
        triangles_uint32=np.asarray(((0, 1, 2), (0, 2, 3)), dtype=np.uint32),
        registration_vertex_indices=np.arange(4, dtype=np.uint32),
        display_vertex_indices=np.arange(4, dtype=np.uint32),
        bounds_min=vertices.min(axis=0),
        bounds_max=vertices.max(axis=0),
    )


def test_surface_ray_returns_exact_local_face_position() -> None:
    """Project World 광선이 맞은 앞쪽 삼각면의 정확한 Source Local 좌표를 반환하는지 확인합니다."""

    import tools.align.viewport.picking as picking
    import tools.align.viewport.point_data as point_data
    from tools.align.alignment.types import Sim3Transform
    from tools.align.constants import MARKER_POINT_KIND_MESH_SURFACE
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    model = _mesh_model()
    runtime.model_repository.add_model(model, identity_display_transform())
    original_runtime = point_data.RUNTIME
    point_data.RUNTIME = runtime
    try:
        view_matrix = np.identity(4, dtype=np.float64)
        view_matrix[2, 3] = -2.0
        region = SimpleNamespace(width=800, height=600)
        region_3d = SimpleNamespace(view_matrix=view_matrix, perspective_matrix=None)
        result = picking._pick_model_mesh_surface_from_ray(
            model.model_id,
            model,
            region,
            region_3d,
            np.asarray((0.25, 0.20, 1.0), dtype=np.float64),
            np.asarray((0.0, 0.0, -1.0), dtype=np.float64),
        )
        assert result is not None
        assert result.point_kind == MARKER_POINT_KIND_MESH_SURFACE
        assert result.source_point_id == 0
        np.testing.assert_allclose(result.position, (0.25, 0.20, 0.0), rtol=0.0, atol=1.0e-12)
        cached_tree = picking._mesh_surface_bvh(model.model_id, model)
        assert cached_tree is picking._mesh_surface_bvh(model.model_id, model)

        translated = np.identity(4, dtype=np.float64)
        translated[:3, 3] = (2.0, 3.0, 4.0)
        runtime.model_repository.set_alignment_transform(
            model.model_id,
            Sim3Transform(matrix_float64=translated),
        )
        translated_view = np.identity(4, dtype=np.float64)
        translated_view[2, 3] = -6.0
        translated_result = picking._pick_model_mesh_surface_from_ray(
            model.model_id,
            model,
            region,
            SimpleNamespace(view_matrix=translated_view, perspective_matrix=None),
            np.asarray((2.25, 3.20, 5.0), dtype=np.float64),
            np.asarray((0.0, 0.0, -1.0), dtype=np.float64),
        )
        assert translated_result is not None
        np.testing.assert_allclose(
            translated_result.position,
            (0.25, 0.20, 0.0),
            rtol=0.0,
            atol=1.0e-12,
        )
        assert cached_tree is picking._mesh_surface_bvh(model.model_id, model)
    finally:
        picking.clear_mesh_surface_pick_cache()
        point_data.RUNTIME = original_runtime


def test_surface_position_validation_and_alignment_use_saved_hit() -> None:
    """면 ID와 좌표가 일치할 때만 마커 정렬 입력으로 사용되는지 확인합니다."""

    from tools.align.alignment.runtime import _validate_saved_source_position
    from tools.align.alignment.types import MarkerInputError
    from tools.align.constants import MARKER_POINT_KIND_MESH_SURFACE, ROLE_REFERENCE
    from tools.align.geometry_access import validated_mesh_surface_position
    from tools.align.state import SelectedPoint

    model = _mesh_model()
    inside = (0.3, 0.2, 0.0)
    np.testing.assert_allclose(
        validated_mesh_surface_position(model, 0, inside),
        inside,
        rtol=0.0,
        atol=0.0,
    )
    selected = SelectedPoint(
        model_role=ROLE_REFERENCE,
        point3d_id=0,
        local_position=inside,
        model_id=model.model_id,
        point_kind=MARKER_POINT_KIND_MESH_SURFACE,
        source_point_id=0,
    )
    np.testing.assert_allclose(
        _validate_saved_source_position(selected, model),
        inside,
        rtol=0.0,
        atol=0.0,
    )
    outside = SelectedPoint(
        model_role=ROLE_REFERENCE,
        point3d_id=0,
        local_position=(0.1, 0.9, 0.0),
        model_id=model.model_id,
        point_kind=MARKER_POINT_KIND_MESH_SURFACE,
        source_point_id=0,
    )
    try:
        _validate_saved_source_position(outside, model)
    except MarkerInputError:
        pass
    else:
        raise AssertionError("삼각면 밖의 저장 좌표를 거부하지 않았습니다.")


def test_same_face_accepts_distinct_positions_but_rejects_duplicate() -> None:
    """하나의 넓은 삼각면에 서로 다른 마커를 놓되 같은 위치 중복은 거부하는지 확인합니다."""

    from tools.align.constants import (
        MARKER_POINT_KIND_MESH_SURFACE,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.state import MarkerPair, MarkerSelectionState, SelectedPoint

    first_reference = SelectedPoint(
        ROLE_REFERENCE,
        0,
        (0.2, 0.2, 0.0),
        "mesh-a",
        MARKER_POINT_KIND_MESH_SURFACE,
        0,
    )
    first_moving = SelectedPoint(
        ROLE_MOVING,
        0,
        (0.25, 0.2, 0.0),
        "mesh-p",
        MARKER_POINT_KIND_MESH_SURFACE,
        0,
    )
    selection = MarkerSelectionState(
        next_pair_id=2,
        completed_pairs=[MarkerPair(1, first_reference, first_moving)],
    )
    assert selection.begin_create_draft()
    assert selection.can_select_draft_point(
        ROLE_REFERENCE,
        0,
        (0.4, 0.2, 0.0),
        model_id="mesh-a",
        point_kind=MARKER_POINT_KIND_MESH_SURFACE,
        source_point_id=0,
    )
    assert not selection.can_select_draft_point(
        ROLE_REFERENCE,
        0,
        (0.2, 0.2, 0.0),
        model_id="mesh-a",
        point_kind=MARKER_POINT_KIND_MESH_SURFACE,
        source_point_id=0,
    )


def _mesh_source(label: str):
    """schema v3 면 마커 codec에 필요한 OBJ source snapshot을 생성합니다."""

    from tools.align.constants import GEOMETRY_KIND_TRIANGLE_MESH
    from tools.align.project_persistence.types import ProjectSourceSnapshot

    path = f"C:/dataset/{label}.obj"
    return ProjectSourceSnapshot(
        selected_root_absolute=path,
        sparse_path_absolute=path,
        sparse_path_relative=f"{label}.obj",
        source_format="obj",
        source_fingerprint="a" * 64,
        display_point_limit=250_000,
        axis_preset="BLENDER",
        axis_forward="Y",
        axis_up="Z",
        display_transform_fingerprint="e" * 32,
        geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
        kind="OBJ",
        path_absolute=path,
        path_relative=f"{label}.obj",
        display_vertex_limit=250_000,
    )


def test_schema_v3_roundtrip_preserves_surface_positions() -> None:
    """기존 v3 면 마커를 현재 저장 형식으로 왕복해 면 ID와 원본 선택 좌표를 보존하는지 확인합니다."""

    from tools.align.constants import MARKER_POINT_KIND_MESH_SURFACE, PROJECT_PERSISTENCE_SCHEMA_VERSION
    from tools.align.display_axes import identity_display_transform
    from tools.align.project_persistence.codec import (
        _snapshot_mapping,
        decode_project_state,
        payload_fingerprint_for_snapshot,
    )
    from tools.align.project_persistence.runtime import _restored_marker_snapshot
    from tools.align.project_persistence.types import (
        ProjectCatalogSnapshot,
        ProjectMarkerPairSnapshot,
        ProjectMarkerPointSnapshot,
        ProjectMarkerSnapshot,
        ProjectModelSnapshot,
        ProjectStateSnapshot,
    )
    from tools.align.state import RuntimeState

    identity = tuple(tuple(float(value) for value in row) for row in np.identity(4))
    surface_reference = ProjectMarkerPointSnapshot(
        "REFERENCE",
        "mesh-a",
        0,
        MARKER_POINT_KIND_MESH_SURFACE,
        0,
        (0.3, 0.2, 0.0),
    )
    surface_moving = ProjectMarkerPointSnapshot(
        "MOVING",
        "mesh-p",
        1,
        MARKER_POINT_KIND_MESH_SURFACE,
        1,
        (0.2, 0.3, 0.0),
    )
    snapshot = ProjectStateSnapshot(
        schema_version=3,
        writer_addon_version=(0, 1, 0),
        project_uuid=str(uuid4()),
        saved_at_utc="2026-08-21T00:00:00Z",
        models=(
            ProjectModelSnapshot(
                "mesh-a",
                "mesh-a",
                1,
                _mesh_source("mesh-a"),
                ProjectCatalogSnapshot("REGISTERED", True, True, None),
                identity,
            ),
            ProjectModelSnapshot(
                "mesh-p",
                "mesh-p",
                2,
                _mesh_source("mesh-p"),
                ProjectCatalogSnapshot("PENDING", False, True, None),
                identity,
            ),
        ),
        markers=ProjectMarkerSnapshot(
            next_pair_id=2,
            active_pair_id=1,
            completed_pairs=(
                ProjectMarkerPairSnapshot(1, surface_reference, surface_moving),
            ),
        ),
    )
    stored = replace(snapshot, payload_fingerprint=payload_fingerprint_for_snapshot(snapshot))
    legacy_payload = json.dumps(
        _snapshot_mapping(stored, include_fingerprint=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    restored = decode_project_state(legacy_payload)
    assert restored.schema_version == PROJECT_PERSISTENCE_SCHEMA_VERSION
    pair = restored.markers.completed_pairs[0]
    assert pair.reference.local_position == (0.3, 0.2, 0.0)
    assert pair.moving.local_position == (0.2, 0.3, 0.0)
    runtime = RuntimeState()
    runtime.model_repository.add_model(_mesh_model("mesh-a"), identity_display_transform())
    runtime.model_repository.add_model(_mesh_model("mesh-p"), identity_display_transform())
    restored_runtime_markers = _restored_marker_snapshot(runtime, restored.markers)
    assert restored_runtime_markers.completed_pairs[0].reference.local_position == (0.3, 0.2, 0.0)
    assert restored_runtime_markers.completed_pairs[0].moving.local_position == (0.2, 0.3, 0.0)


def run_all() -> None:
    """메시 면 마커 회귀 테스트를 Blender 실행 환경에서 순서대로 실행합니다."""

    _add_source_path()
    test_surface_ray_returns_exact_local_face_position()
    test_surface_position_validation_and_alignment_use_saved_hit()
    test_same_face_accepts_distinct_positions_but_rejects_duplicate()
    test_schema_v3_roundtrip_preserves_surface_positions()
    print("test_mesh_surface_picking: 통과")


if __name__ == "__main__":
    run_all()
