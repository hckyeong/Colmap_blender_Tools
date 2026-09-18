"""CCCoreLib 형상 입력의 출처·삼각면·공통 좌표·불변 지문 계약을 검증합니다."""

from __future__ import annotations

import builtins
import hashlib
from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np


def _add_test_paths() -> None:
    """실험 소스와 기존 형상 자료를 Blender의 탐색 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _add_layer(runtime, model_id: str, *, standalone: bool = False) -> None:
    """희소점과 별도 삼각면을 가진 연결 또는 독립 메시 레이어를 만듭니다."""
    from test_companion_display import _colmap, _mesh
    from tools.align.display_axes import identity_display_transform
    from tools.align.model_repository import CompanionMeshSourceMetadata

    mesh = _mesh(model_id)
    vertices = np.vstack((mesh.vertices_float64, ((10.0, 0.0, 3.0),)))
    mesh = replace(mesh, vertices_float64=vertices,
                   triangles_uint32=np.asarray(((0, 1, 2), (0, 1, 3)), dtype=np.uint32),
                   triangle_texcoord_indices_int32=np.empty((0, 3), dtype=np.int32),
                   triangle_material_indices_int32=np.empty(0, dtype=np.int32),
                   triangle_polygon_indices_uint32=np.asarray((0, 1), dtype=np.uint32),
                   triangle_loop_indices_uint32=np.arange(6, dtype=np.uint32).reshape((2, 3)),
                   original_polygon_count=2,
                   bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0))
    model = mesh if standalone else _colmap(model_id)
    runtime.model_repository.add_model(model, identity_display_transform())
    runtime.model_catalog.add_imported_model(
        model_id, model_id, select_after_import=True,
        geometry_kind="TRIANGLE_MESH" if standalone else "COLMAP_POINT_CLOUD")
    if not standalone:
        runtime.model_repository.add_companion_mesh(
            model_id, mesh, CompanionMeshSourceMetadata(str(mesh.source_path), "a" * 64),
            identity_display_transform())


def _runtime(*, standalone_moving: bool = False):
    """실제 카탈로그·저장소·마커 상태를 사용하는 최소 A/P 런타임을 만듭니다."""
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    _add_layer(runtime, "기준")
    _add_layer(runtime, "이동", standalone=standalone_moving)
    return runtime


def _snapshot(runtime, initial=None):
    """현재 선택된 P와 A 전체를 같은 공개 형상 입력 API에 전달합니다."""
    from tools.align.alignment.icp_geometry import create_cccore_geometry_snapshot

    moving_id = runtime.model_catalog.selected_moving_model_id
    if initial is None:
        initial = runtime.model_repository.alignment_transform_for_model_id(moving_id).matrix_float64
    return create_cccore_geometry_snapshot(
        runtime, moving_model_id=moving_id,
        reference_model_ids=tuple(item.model_id for item in runtime.model_catalog.active_models()),
        initial_alignment_float64=initial)


def _select(runtime, model_id: str) -> None:
    """기존 공개 선택 경계를 통해 P 레이어 하나를 선택합니다."""
    assert runtime.model_catalog.select_model(model_id, has_marker_draft=False, completed_pair_count=0)


def _surface(runtime, model_id: str) -> None:
    """눈 상태를 유지하면서 레이어의 입력 형상을 메시로 선택합니다."""
    runtime.model_catalog.set_geometry_visibility(model_id, show_surface=True)


def _expect_error(operation, contains: str) -> None:
    """잘못된 입력이 원인에 맞는 명시적 오류로 차단되는지 확인합니다."""
    try:
        operation()
    except (ValueError, RuntimeError) as error:
        assert contains in str(error), str(error)
    else:
        raise AssertionError(f"오류가 발생해야 합니다: {contains}")


def _project(positions, alignment, axes):
    """독립적인 동차 좌표 계산으로 C 곱하기 N의 기대값을 구합니다."""
    homogeneous = np.column_stack((positions, np.ones(len(positions))))
    return (alignment @ axes @ homogeneous.T).T[:, :3]


def test_point_selection_uses_sparse_and_preserves_source() -> None:
    """연결 메시가 있어도 점군 선택은 희소 등록점만 사용하며 원본을 보존합니다."""
    from tools.align.alignment.icp_geometry import geometry_mode_summary

    runtime = _runtime()
    raw = runtime.model_repository.models["이동"].points.positions
    before = raw.copy()
    snapshot = _snapshot(runtime)
    np.testing.assert_array_equal(snapshot.moving.positions_float64, raw)
    assert snapshot.moving.triangles_uint32 is None
    assert snapshot.moving.geometry_source == "MAIN"
    assert snapshot.geometry_mode == "POINT_TO_POINT" and not snapshot.reverse_registration
    assert snapshot.reference_combined is snapshot.references[0]
    assert "점군" in geometry_mode_summary(runtime)
    np.testing.assert_array_equal(raw, before)
    assert not np.shares_memory(snapshot.moving.positions_float64, raw)


def test_companion_keeps_faces_and_uses_independent_axes() -> None:
    """연결 정점 전체·삼각면과 독립 축 N을 P 초기 C 및 A 확정 C에 적용합니다."""
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.display_axes import create_model_display_transform

    runtime = _runtime()
    repository = runtime.model_repository
    for model_id in ("기준", "이동"):
        _surface(runtime, model_id)
        repository.set_display_transform(model_id, create_model_display_transform("REALITY_CAPTURE", "Z", "-Y"))
        repository.set_companion_display_transform(model_id, create_model_display_transform(
            "REALITY_CAPTURE_OBJ", "Y", "Z", rotation_xyz_degrees=(23, -31, 47)))
    reference_alignment = create_sim3_transform(np.eye(3), 1.75, np.array((3.0, -8.0, 11.0)))
    initial = create_sim3_transform(np.array(((0, -1, 0), (1, 0, 0), (0, 0, 1))),
                                    2.5, np.array((-2.0, 6.0, 13.0)))
    repository.set_alignment_transform("기준", reference_alignment)
    snapshot = _snapshot(runtime, initial.matrix_float64)
    assert snapshot.geometry_mode == "MESH_TO_MESH"
    assert snapshot.moving.owner_model_ids == ("이동",)
    assert snapshot.moving.geometry_source == "COMPANION"
    assert len(snapshot.moving.positions_float64) == 4
    for model_id, geometry, alignment in (("이동", snapshot.moving, initial),
                                           ("기준", snapshot.references[0], reference_alignment)):
        mesh = repository.companion_meshes[model_id]
        axes = repository.companion_display_transform_for_model_id(model_id)
        np.testing.assert_allclose(geometry.positions_float64,
            _project(mesh.vertices_float64, alignment.matrix_float64, axes.matrix_float64), atol=1e-12)
        np.testing.assert_array_equal(geometry.triangles_uint32, mesh.triangles_uint32)
    assert snapshot.provenance.moving.committed_alignment_float64 != snapshot.provenance.initial_alignment_float64
    json.dumps(snapshot.provenance.to_mapping(), ensure_ascii=False, allow_nan=False)


def test_standalone_and_cross_kind_modes() -> None:
    """독립 메시는 주 형상을 사용하고 P/A의 서로 다른 종류를 허용합니다."""
    runtime = _runtime(standalone_moving=True)
    mesh_to_point = _snapshot(runtime)
    assert mesh_to_point.geometry_mode == "MESH_TO_POINT" and mesh_to_point.reverse_registration
    assert mesh_to_point.moving.geometry_source == "MAIN"
    np.testing.assert_array_equal(mesh_to_point.moving.triangles_uint32,
                                  runtime.model_repository.models["이동"].triangles_uint32)
    runtime = _runtime()
    _surface(runtime, "기준")
    point_to_mesh = _snapshot(runtime)
    assert point_to_mesh.geometry_mode == "POINT_TO_MESH" and not point_to_mesh.reverse_registration


def test_hidden_multiple_references_and_triangle_offsets() -> None:
    """숨긴 연결·독립 A 메시도 각각 변환한 뒤 정점 오프셋으로 합칩니다."""
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.alignment.icp_geometry import geometry_snapshot_validation_message

    runtime = _runtime()
    _surface(runtime, "기준")
    _add_layer(runtime, "숨긴독립기준", standalone=True)
    runtime.model_catalog.promote_model_for_icp_commit("숨긴독립기준")
    runtime.model_catalog.set_model_visibility("숨긴독립기준", False)
    shift = create_sim3_transform(np.eye(3), 3.0, np.array((20.0, 30.0, -9.0)))
    runtime.model_repository.set_alignment_transform("숨긴독립기준", shift)
    _select(runtime, "이동")
    snapshot = _snapshot(runtime)
    combined = snapshot.reference_combined
    assert combined.owner_model_ids == ("기준", "숨긴독립기준")
    assert combined.geometry_source == "MIXED_SOURCE"
    assert len(combined.positions_float64) == 8
    np.testing.assert_array_equal(combined.triangles_uint32, ((0, 1, 2), (0, 1, 3), (4, 5, 6), (4, 5, 7)))
    np.testing.assert_allclose(combined.positions_float64[4:],
        _project(runtime.model_repository.models["숨긴독립기준"].vertices_float64, shift.matrix_float64, np.eye(4)))
    runtime.model_catalog.set_model_visibility("숨긴독립기준", True)
    assert geometry_snapshot_validation_message(runtime, snapshot) is None


def test_mixed_reference_and_wrong_selection_are_blocked_without_hashing() -> None:
    """A 종류 혼합은 레이어 이름으로 설명하고 화면 검사는 대형 배열을 읽지 않습니다."""
    from tools.align.alignment import icp_geometry

    runtime = _runtime()
    _add_layer(runtime, "추가메시", standalone=True)
    runtime.model_catalog.promote_model_for_icp_commit("추가메시")
    _select(runtime, "이동")
    with patch.object(icp_geometry, "_array_fingerprint", side_effect=AssertionError("해시 호출 금지")):
        reason = icp_geometry.geometry_selection_block_reason(runtime)
        assert "기준" in reason and "추가메시" in reason and "혼합" in reason
    _surface(runtime, "기준")
    with patch.object(icp_geometry, "_array_fingerprint", side_effect=AssertionError("해시 호출 금지")):
        assert icp_geometry.geometry_selection_block_reason(runtime) is None
    _expect_error(lambda: icp_geometry.create_cccore_geometry_snapshot(
        runtime, moving_model_id="이동", reference_model_ids=("기준",), initial_alignment_float64=np.eye(4)), "A 목록")
    _expect_error(lambda: icp_geometry.create_cccore_geometry_snapshot(
        runtime, moving_model_id="추가메시", reference_model_ids=("기준", "추가메시"), initial_alignment_float64=np.eye(4)), "P 레이어")


def _marker_pair(runtime, *, moving_mesh: bool = True):
    """서로 다른 역할의 점군·연결 면 마커를 실제 마커 자료 구조로 만듭니다."""
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING
    from tools.align.state import MarkerPair, SelectedPoint

    reference = SelectedPoint(ROLE_REFERENCE, 11, (0, 0, 0), "기준")
    moving = (SelectedPoint(ROLE_MOVING, 0, (10.5, 0.5, 0), "이동", "MESH_SURFACE", 0, "COMPANION")
              if moving_mesh else SelectedPoint(ROLE_MOVING, 11, (0, 0, 0), "이동"))
    pair = MarkerPair(1, reference, moving)
    runtime.marker_selection.completed_pairs.append(pair)
    return pair


def test_marker_roles_and_source_slots_are_preserved() -> None:
    """역할별 서로 다른 마커 종류는 허용하되 선택 종류·원본 슬롯 재해석을 막습니다."""
    from tools.align.alignment.icp_geometry import geometry_selection_block_reason

    runtime = _runtime()
    _surface(runtime, "이동")
    pair = _marker_pair(runtime)
    assert geometry_selection_block_reason(runtime) is None
    snapshot = _snapshot(runtime)
    assert snapshot.geometry_mode == "MESH_TO_POINT"
    runtime.model_catalog.set_geometry_visibility("이동", show_points=True)
    assert "P 마커" in geometry_selection_block_reason(runtime)
    _surface(runtime, "이동")
    runtime.marker_selection.completed_pairs[:] = [replace(pair, moving=replace(pair.moving, geometry_source="MAIN"))]
    assert "현재 선택 형상" in geometry_selection_block_reason(runtime)
    runtime.marker_selection.completed_pairs[:] = [pair]
    _surface(runtime, "기준")
    assert "A 마커" in geometry_selection_block_reason(runtime)


def _mutable_geometry(runtime, *, mesh: bool):
    """잘못된 외부 원본과 사후 변조를 검사할 가변 테스트 자료를 연결합니다."""
    repository = runtime.model_repository
    if mesh:
        original = repository.companion_meshes["이동"]
        model = SimpleNamespace(geometry_kind="TRIANGLE_MESH", model_id="이동", geometry_revision=1,
            source_path=original.source_path, vertices_float64=original.vertices_float64.copy(),
            triangles_uint32=original.triangles_uint32.copy())
        repository.companion_meshes["이동"] = model
        _surface(runtime, "이동")
    else:
        original = repository.models["이동"]
        model = SimpleNamespace(model_id="이동", geometry_revision=1, source_path=original.source_path,
                                points=SimpleNamespace(positions=original.points.positions.copy()))
        repository.models["이동"] = model
    return model


def test_invalid_raw_geometry_reports_specific_errors() -> None:
    """누락·빈 면·잘못된 인덱스·비유한 좌표·퇴화 입력을 명시적으로 거부합니다."""
    for issue in ("누락", "빈면", "음수", "초과", "실수", "비유한", "무한", "퇴화"):
        runtime = _runtime()
        model = _mutable_geometry(runtime, mesh=True)
        if issue == "누락":
            model.triangles_uint32 = None
            expected = "삼각면 배열이 없습니다"
        elif issue == "빈면":
            model.triangles_uint32 = np.empty((0, 3), dtype=np.uint32)
            expected = "비었거나"
        elif issue in {"음수", "초과"}:
            model.triangles_uint32 = np.array(((-1 if issue == "음수" else 2**32, 1, 2),), dtype=np.int64)
            expected = "인덱스"
        elif issue == "실수":
            model.triangles_uint32 = np.array(((0.0, 1.5, 2.0),))
            expected = "정수"
        elif issue in {"비유한", "무한"}:
            model.vertices_float64[0, 0] = np.nan if issue == "비유한" else np.inf
            expected = "유한하지"
        else:
            model.triangles_uint32[:] = (0, 0, 2)
            expected = "퇴화"
        _expect_error(lambda: _snapshot(runtime), expected)
    runtime = _runtime()
    model = _mutable_geometry(runtime, mesh=False)
    model.points.positions[:] = ((0, 0, 0), (1, 0, 0), (2, 0, 0))
    _expect_error(lambda: _snapshot(runtime), "퇴화 점군")
    runtime.model_repository.companion_meshes.pop("이동")
    _surface(runtime, "이동")
    _expect_error(lambda: _snapshot(runtime), "연결 메시 원본이 없습니다")


def _install_mixed_faces(runtime, model_id: str, *, standalone: bool = False):
    """반복 인덱스·동일 좌표·공선면 사이의 정상면을 읽기 전용 원본으로 설치합니다."""
    mapping = runtime.model_repository.models if standalone else runtime.model_repository.companion_meshes
    original = mapping[model_id]
    vertices = np.asarray(((10, 0, 0), (12, 0, 0), (10, 2, 0), (10, 0, 3),
                           (10, 0, 0), (11, 0, 0)), dtype=np.float64)
    triangles = np.asarray(((0, 0, 2), (0, 1, 2), (0, 4, 2), (0, 5, 1), (0, 1, 3)), dtype=np.uint32)
    vertices.setflags(write=False)
    triangles.setflags(write=False)
    model = SimpleNamespace(
        geometry_kind="TRIANGLE_MESH", model_id=model_id, geometry_revision=1,
        source_path=original.source_path, vertices_float64=vertices, triangles_uint32=triangles,
    )
    mapping[model_id] = model
    _surface(runtime, model_id)
    return model


def _source_digest(array, dtype: str) -> str:
    """원본 전체의 표준 바이트를 독립 해시하여 출처 지문을 확인합니다."""
    digest = hashlib.sha256()
    digest.update(f"{dtype}:{array.shape}".encode("ascii"))
    digest.update(np.asarray(array, dtype=dtype).tobytes())
    return digest.hexdigest()


def test_degenerate_faces_filter_snapshot_only_and_preserve_marker_ids() -> None:
    """독립·연결 메시의 정렬 입력만 압축하고 원본 지문·정점·마커 ID·불변성을 보존합니다."""
    from tools.align.alignment.icp_geometry import geometry_snapshot_validation_message

    for standalone in (False, True):
        runtime = _runtime(standalone_moving=standalone)
        model = _install_mixed_faces(runtime, "이동", standalone=standalone)
        pair = _marker_pair(runtime)
        pair = replace(pair, moving=replace(
            pair.moving, point3d_id=1, source_point_id=1,
            geometry_source="MAIN" if standalone else "COMPANION",
        ))
        runtime.marker_selection.completed_pairs[:] = [pair]
        original_faces = model.triangles_uint32.copy()
        original_positions = model.vertices_float64.copy()
        snapshot = _snapshot(runtime)
        np.testing.assert_array_equal(snapshot.moving.triangles_uint32, original_faces[[1, 4]])
        np.testing.assert_array_equal(snapshot.moving.positions_float64, original_positions[:4])
        source = snapshot.provenance.moving
        assert source.point_count == 6 and source.triangle_count == 5
        assert source.positions_fingerprint == _source_digest(original_positions, "<f8")
        assert source.triangles_fingerprint == _source_digest(original_faces, "<u4")
        assert runtime.marker_selection.completed_pairs[0] is pair
        assert pair.moving.source_point_id == 1
        np.testing.assert_array_equal(model.triangles_uint32, original_faces)
        np.testing.assert_array_equal(model.vertices_float64, original_positions)
        for array in (model.triangles_uint32, model.vertices_float64,
                      snapshot.moving.triangles_uint32, snapshot.moving.positions_float64):
            assert not array.flags.writeable
        metadata = snapshot.without_arrays()
        assert geometry_snapshot_validation_message(runtime, metadata, verify_content=True) is None
        changed = original_faces.copy()
        changed[0] = (1, 1, 2)
        model.triangles_uint32 = changed
        assert geometry_snapshot_validation_message(runtime, metadata, verify_content=True) is not None
        assert geometry_snapshot_validation_message(runtime, metadata, verify_content=False) is None
        model.triangles_uint32 = original_faces
        changed_positions = original_positions.copy()
        changed_positions[5, 0] = 11.5
        model.vertices_float64 = changed_positions
        assert geometry_snapshot_validation_message(runtime, metadata, verify_content=True) is not None
        assert geometry_snapshot_validation_message(runtime, metadata, verify_content=False) is None
        model.vertices_float64 = original_positions
        assert geometry_snapshot_validation_message(runtime, metadata, verify_content=True) is None
        np.testing.assert_array_equal(snapshot.moving.triangles_uint32, original_faces[[1, 4]])


def test_single_valid_face_survives_large_origin_and_small_uniform_scale() -> None:
    """큰 원점과 작은 균일 배율에서도 정상면 하나를 보존하고 공선면만 제외합니다."""
    from tools.align.alignment.types import create_sim3_transform

    for offset, scale in ((1e12, 1.0), (0.0, 1e-9), (1e12, 1e-9)):
        runtime = _runtime()
        model = _install_mixed_faces(runtime, "이동")
        model.vertices_float64 = model.vertices_float64 + offset
        model.triangles_uint32 = model.triangles_uint32[:4]
        initial = create_sim3_transform(np.eye(3), scale, np.zeros(3))
        snapshot = _snapshot(runtime, initial.matrix_float64)
        np.testing.assert_array_equal(snapshot.moving.triangles_uint32, ((0, 1, 2),))
        np.testing.assert_allclose(snapshot.moving.positions_float64,
                                   model.vertices_float64[:3] * scale, rtol=0, atol=0)
        assert snapshot.provenance.moving.triangle_count == 4
    runtime = _runtime()
    model = _install_mixed_faces(runtime, "이동")
    model.vertices_float64 = model.vertices_float64 * 1e-12
    snapshot = _snapshot(runtime)
    np.testing.assert_array_equal(snapshot.moving.triangles_uint32, ((0, 1, 2), (0, 1, 3)))


def test_filter_across_triangle_blocks_and_mixed_reference_sources() -> None:
    """블록 양쪽 정상면과 연결·독립 A의 압축 정점 오프셋 및 원본 출처를 보존합니다."""
    from tools.align.alignment.icp_geometry import geometry_snapshot_validation_message

    runtime = _runtime()
    first = _install_mixed_faces(runtime, "기준")
    faces = np.tile(np.asarray((0, 0, 2), dtype=np.uint32), (65_538, 1))
    faces[65_535] = (0, 1, 2)
    faces[65_536] = (0, 1, 3)
    faces.setflags(write=False)
    first.triangles_uint32 = faces
    _add_layer(runtime, "추가독립기준", standalone=True)
    _install_mixed_faces(runtime, "추가독립기준", standalone=True)
    runtime.model_catalog.promote_model_for_icp_commit("추가독립기준")
    _select(runtime, "이동")
    snapshot = _snapshot(runtime)
    np.testing.assert_array_equal(snapshot.references[0].triangles_uint32, ((0, 1, 2), (0, 1, 3)))
    np.testing.assert_array_equal(snapshot.reference_combined.triangles_uint32,
                                  ((0, 1, 2), (0, 1, 3), (4, 5, 6), (4, 5, 7)))
    assert len(snapshot.reference_combined.positions_float64) == 8
    assert snapshot.reference_combined.geometry_source == "MIXED_SOURCE"
    assert tuple(source.triangle_count for source in snapshot.reference_combined.provenance.sources) == (65_538, 5)
    assert first.triangles_uint32 is faces and not faces.flags.writeable
    assert geometry_snapshot_validation_message(runtime, snapshot.without_arrays(), verify_content=True) is None


def test_unused_outlier_vertices_are_removed_only_from_alignment_geometry() -> None:
    """퇴화면의 외딴 정점과 중간 미참조 정점을 정렬 입력에서만 제거하고 면을 재색인합니다."""
    runtime = _runtime()
    model = _install_mixed_faces(runtime, "이동")
    vertices = np.asarray(((1e12, 1e12, 1e12), (10, 0, 0), (10, 0, 0),
                           (12, 0, 0), (10, 2, 0)), dtype=np.float64)
    faces = np.asarray(((0, 0, 0), (1, 3, 4), (1, 2, 4)), dtype=np.uint32)
    vertices.setflags(write=False)
    faces.setflags(write=False)
    model.vertices_float64 = vertices
    model.triangles_uint32 = faces
    snapshot = _snapshot(runtime)
    np.testing.assert_array_equal(snapshot.moving.positions_float64, vertices[[1, 3, 4]])
    np.testing.assert_array_equal(snapshot.moving.triangles_uint32, ((0, 1, 2),))
    assert model.vertices_float64 is vertices and model.triangles_uint32 is faces
    assert snapshot.provenance.moving.point_count == 5
    assert snapshot.provenance.moving.triangle_count == 3
    assert snapshot.provenance.moving.positions_fingerprint == _source_digest(vertices, "<f8")
    assert snapshot.provenance.moving.triangles_fingerprint == _source_digest(faces, "<u4")


def test_immutable_arrays_and_small_metadata_detect_content_changes() -> None:
    """원본 수정이 스냅샷을 바꾸지 않고 배열 없는 지문 검사에서 발견되는지 확인합니다."""
    from tools.align.alignment.icp_geometry import geometry_snapshot_validation_message

    runtime = _runtime()
    model = _mutable_geometry(runtime, mesh=True)
    snapshot = _snapshot(runtime)
    metadata = snapshot.without_arrays()
    assert not hasattr(metadata, "moving") and metadata.moving_model_id == "이동"
    assert metadata.reference_model_ids == ("기준",)
    assert model.vertices_float64.flags.writeable and model.triangles_uint32.flags.writeable
    for array in (snapshot.moving.positions_float64, snapshot.moving.triangles_uint32):
        assert not array.flags.writeable
        _expect_error(lambda: array.setflags(write=True), "WRITEABLE")
    before = snapshot.moving.positions_float64.copy()
    model.vertices_float64[0, 0] += 0.25
    np.testing.assert_array_equal(snapshot.moving.positions_float64, before)
    assert geometry_snapshot_validation_message(runtime, metadata) is not None
    assert geometry_snapshot_validation_message(runtime, metadata, verify_content=False) is None
    model.vertices_float64[0, 0] -= 0.25
    model.triangles_uint32[0] = (0, 2, 1)
    assert geometry_snapshot_validation_message(runtime, metadata) is not None
    model.triangles_uint32[0] = (0, 1, 2)
    assert geometry_snapshot_validation_message(runtime, metadata) is None
    model.geometry_revision += 1
    assert geometry_snapshot_validation_message(runtime, metadata, verify_content=False) is not None


def test_axes_initial_alignment_marker_and_catalog_invalidate_metadata() -> None:
    """축·초기 정렬·마커·A 추가 변경이 작은 지문 재검사에서 누락되지 않습니다."""
    from tools.align.alignment.icp_geometry import geometry_snapshot_validation_message
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.display_axes import create_model_display_transform

    for changed in ("축", "정렬", "초기", "마커", "A추가"):
        runtime = _runtime()
        pair = _marker_pair(runtime, moving_mesh=False)
        metadata = _snapshot(runtime).without_arrays()
        initial = None
        if changed == "축":
            runtime.model_repository.set_display_transform("이동", create_model_display_transform("REALITY_CAPTURE", "Z", "-Y"))
        elif changed in {"정렬", "초기"}:
            transform = create_sim3_transform(np.eye(3), 1.0, np.array((1.0, 2.0, 3.0)))
            if changed == "초기":
                initial = transform.matrix_float64
            else:
                runtime.model_repository.set_alignment_transform("기준", transform)
        elif changed == "마커":
            runtime.marker_selection.completed_pairs[:] = [replace(pair, moving=replace(pair.moving, local_position=(0.1, 0, 0)))]
        else:
            _add_layer(runtime, "새기준")
            runtime.model_catalog.promote_model_for_icp_commit("새기준")
            _select(runtime, "이동")
        assert geometry_snapshot_validation_message(runtime, metadata, initial_alignment_float64=initial,
                                                    verify_content=False) is not None


def test_pure_module_does_not_import_blender() -> None:
    """새 패키지 공간에서 형상 모듈의 전이 의존성까지 Blender 임포트를 금지합니다."""
    source = Path(__file__).resolve().parents[3] / "tools" / "align"
    alias = "_cccore_geometry_pure"
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        """순수 형상 모듈에서 Blender 전용 모듈을 요청하면 즉시 실패시킵니다."""
        if name == "bpy" or name.startswith("bpy.") or name == "bpy_extras" or name.startswith("bpy_extras."):
            raise AssertionError(f"Blender 의존성 유입: {name}")
        return original_import(name, globals, locals, fromlist, level)

    for name, path in ((alias, source), (alias + ".alignment", source / "alignment")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    try:
        with patch.object(builtins, "__import__", side_effect=guarded_import):
            module = importlib.import_module(alias + ".alignment.icp_geometry")
        assert module.CcCoreGeometrySnapshot is not None
    finally:
        for name in tuple(sys.modules):
            if name == alias or name.startswith(alias + "."):
                sys.modules.pop(name, None)


def main() -> None:
    """실제 Blender에서 형상 입력의 모든 회귀 검사를 순서대로 실행합니다."""
    _add_test_paths()
    tests = tuple(value for name, value in globals().items() if name.startswith("test_") and callable(value))
    for test in tests:
        test()
    print(f"CCCoreLib 형상 입력: {len(tests)}개 검사 통과")


if __name__ == "__main__":
    main()
