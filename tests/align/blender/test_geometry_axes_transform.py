"""축 설정의 중심 변환·비율 잠금·저장·정렬 입력을 실제 Blender에서 검증합니다."""

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np


def _bootstrap():
    """제품 소스와 기존 실제 런타임 시험 자료를 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        sys.path.insert(0, str(path))


def test_center_rotation_scale_and_world_origin():
    """축 변환 후 중심 유지·XYZ 배율과 기존 정렬을 포함한 원점 이동을 검증합니다."""
    from test_geometry_axes_runtime import _fixture
    from tools.align.axis_settings import axis_transform_center, centered_axis_transform, axis_transform_at_world_origin
    from tools.align.display_axes import create_model_display_transform, display_position
    from tools.align.alignment.types import create_sim3_transform

    with _fixture() as runtime:
        original = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(13.1, 29.2, -43.3))
        centered = centered_axis_transform(runtime, "moving", original)
        np.testing.assert_allclose(centered.matrix_float64, original.matrix_float64, atol=1e-12)
        center = axis_transform_center(runtime, "moving")
        changed = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(51, -7, 94),
            scale_xyz=(2, 3, 4), translation_xyz=centered.translation_xyz, pivot_source=center)
        np.testing.assert_allclose(display_position(changed, center), display_position(original, center), atol=1e-12)
        linear = changed.matrix_float64[:3, :3]
        np.testing.assert_allclose(sorted(np.linalg.svd(linear, compute_uv=False)), (2, 3, 4), atol=1e-12)
        alignment = create_sim3_transform(uniform_scale=2.5, rotation_float64=np.eye(3), translation_float64=np.array((11, -19, 28)))
        runtime.model_repository.set_alignment_transform("moving", alignment)
        moved = axis_transform_at_world_origin(runtime, "moving", changed)
        world = alignment.matrix_float64 @ np.append(display_position(moved, center), 1)
        np.testing.assert_allclose(world[:3], 0, atol=1e-12)
        np.testing.assert_array_equal(moved.matrix_float64[:3, :3], changed.matrix_float64[:3, :3])


def test_popup_independent_linked_scale_and_noop():
    """실제 팝업 경계에서 독립 배율·비율 유지·미리보기 취소·무변경 적용을 검증합니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align.display_axes import create_model_display_transform

    with _fixture() as (runtime, context):
        original = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(13.123456789, -2.987654321, 76.111111111))
        runtime.model_repository.set_display_transform("moving", original)
        dialog = _open("moving", context)
        assert runtime.axis_settings.main_transform is original
        assert ui.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
        assert runtime.undo_journal.undo_count == 0
        dialog = _open("moving", context)
        companion = runtime.axis_settings.companion_transform
        dialog.main_scale = (2, 3, 4)
        ui._update_main_scale(dialog, context)
        np.testing.assert_allclose(runtime.axis_settings.main_transform.scale_xyz, (2, 3, 4))
        dialog.main_scale_locked = True
        dialog.main_scale = (2, 6, 4)
        ui._update_main_scale(dialog, context)
        np.testing.assert_allclose(dialog.main_scale, (4, 6, 8))
        dialog.main_scale_locked = False
        dialog.main_scale = (4, 6, 12)
        ui._update_main_scale(dialog, context)
        np.testing.assert_allclose(dialog.main_scale, (4, 6, 12))
        assert runtime.axis_settings.companion_transform is companion
        origin = SimpleNamespace(edit_token=dialog.edit_token, component="MAIN", report=dialog.report)
        assert ui.COLMAPALIGN_OT_center_geometry_at_origin.execute(origin, context) == {"FINISHED"}
        from tools.align.axis_settings import axis_transform_center
        center = axis_transform_center(runtime, "moving")
        world = runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64 @ runtime.axis_settings.main_transform.matrix_float64 @ np.append(center, 1)
        np.testing.assert_allclose(world[:3], 0, atol=1e-12)
        dialog.cancel(context)
        assert runtime.model_repository.display_transform_for_model_id("moving") is original
        assert runtime.undo_journal.undo_count == 0


def test_transform_commit_undo_redo():
    """이동·배율 적용과 되돌리기가 원본과 공통 정렬을 유지하는지 검증합니다."""
    from test_geometry_axes_runtime import _fixture
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, commit_axis_edit
    from tools.align.display_axes import create_model_display_transform
    from tools.align import session

    with _fixture() as runtime:
        repository = runtime.model_repository
        original = repository.display_transform_for_model_id("moving")
        alignment = repository.alignment_transform_for_model_id("moving")
        raw = repository.models["moving"].points.positions.copy()
        new = create_model_display_transform("BLENDER", "Y", "Z", translation_xyz=(2, 3, 4), scale_xyz=(2, 3, 4), pivot_source=(4, 5, 6))
        token = begin_axis_edit(runtime, "moving")
        update_axis_edit(runtime, token, main_transform=new, companion_transform=runtime.axis_settings.companion_transform)
        assert commit_axis_edit(runtime, token)
        assert runtime.undo_journal.undo_count == 1
        assert session.undo_session_state()
        assert repository.display_transform_for_model_id("moving") is original
        assert session.redo_session_state()
        assert repository.display_transform_for_model_id("moving") is new
        assert repository.alignment_transform_for_model_id("moving") is alignment
        np.testing.assert_array_equal(raw, repository.models["moving"].points.positions)


def test_persistence_affine_and_legacy():
    """새 변환의 저장 왕복과 구버전 지문 보존 및 잘못된 배율 거부를 검증합니다."""
    from tools.align.display_axes import create_model_display_transform
    from tools.align.project_persistence.runtime import _project_axes_snapshot, _display_transform_from_axes
    from tools.align.project_persistence.codec import _axes_mapping, _decode_axes
    from test_geometry_axes_runtime import _expect_error

    transform = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(11, 22, 33),
        translation_xyz=(0.123456789, 900, -52), scale_xyz=(0.2, 3.5, 10), pivot_source=(20, 10, -7))
    snapshot = _project_axes_snapshot(transform)
    restored = _display_transform_from_axes(_decode_axes(_axes_mapping(snapshot), "axes"))
    assert restored.fingerprint == transform.fingerprint
    np.testing.assert_array_equal(restored.matrix_float64, transform.matrix_float64)
    legacy = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(11, 22, 33))
    old = _project_axes_snapshot(legacy)
    for version in (5, 6, 7, 8):
        decoded = _decode_axes(_axes_mapping(old, version), "axes", version)
        assert _display_transform_from_axes(decoded).fingerprint == legacy.fingerprint
    _expect_error(lambda: _axes_mapping(snapshot, 8))
    for scale in ((0, 1, 1), (-1, 1, 1), (float("nan"), 1, 1), (1, 2)):
        _expect_error(lambda: create_model_display_transform("BLENDER", "Y", "Z", scale_xyz=scale))
    _expect_error(lambda: _decode_axes(_axes_mapping(replace(snapshot, scale_xyz=(0, 1, 1))), "axes"))


def test_full_project_restore_with_affine_axes():
    """실제 프로젝트 저장·원본 복원 경로에서도 두 형상의 중심 변환을 보존합니다."""
    from unittest.mock import patch
    import test_geometry_axes_persistence as regression
    from tools.align.display_axes import create_model_display_transform

    def set_transforms(runtime):
        """복원 시험의 주 형상과 연결 메시를 서로 다른 중심 변환으로 준비합니다."""
        values = []
        for companion in (False, True):
            value = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(11, 22, 33),
                translation_xyz=(8, 3, 2) if companion else (-2, -3, -4), scale_xyz=(2, 3, 4), pivot_source=(6, 7, 8))
            setter = runtime.model_repository.set_companion_display_transform if companion else runtime.model_repository.set_display_transform
            setter("owner", value)
            values.append(value)
        return tuple(values)

    with patch.object(regression, "_set_axes", set_transforms):
        regression.test_independent_axes_roundtrip_preserves_source_and_prepares_final_axes()


def test_icp_and_obj_use_affine_display():
    """비균일 표시 배율은 정렬 입력과 OBJ에 반영하고 공통 정렬의 제약은 유지합니다."""
    from test_icp_cccore_geometry import _runtime, _snapshot
    from tools.align.display_axes import create_model_display_transform, display_positions
    from tools.align.alignment.icp_geometry import _matrix_tuple
    from tools.align.mesh_io.obj_writer import compose_project_world_matrix
    from test_geometry_axes_runtime import _expect_error

    runtime = _runtime()
    transform = create_model_display_transform("BLENDER", "Y", "Z", translation_xyz=(2, 3, 4), scale_xyz=(2, 3, 4), pivot_source=(10, 20, 30))
    runtime.model_repository.set_display_transform("이동", transform)
    snapshot = _snapshot(runtime)
    expected = display_positions(transform, runtime.model_repository.models["이동"].points.positions)
    np.testing.assert_allclose(snapshot.moving.positions_float64, expected)
    np.testing.assert_array_equal(compose_project_world_matrix(np.eye(4), transform), transform.matrix_float64)
    _expect_error(lambda: _matrix_tuple(transform.matrix_float64))


def test_large_coordinates_and_scale_limits():
    """큰 중심 좌표와 입력 한계 배율에서도 유효한 변환과 OBJ 내보내기를 허용합니다."""
    from tools.align.display_axes import create_model_display_transform
    from tools.align.mesh_io.obj_writer import compose_project_world_matrix

    for scale in ((1e-6, 1e-6, 1e-6), (1e6, 1e6, 1e6), (0.001, 1000, 2)):
        transform = create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(17, -29, 43),
            translation_xyz=(1e6, -2e6, 3e6), scale_xyz=scale, pivot_source=(1e5, 2e5, -3e5))
        matrix = compose_project_world_matrix(np.eye(4), transform)
        assert np.isfinite(matrix).all()


def run_all():
    """중심 변환과 호환성 회귀 검사를 순서대로 실행합니다."""
    _bootstrap()
    for name, function in tuple(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print("test_geometry_axes_transform: 통과")


if __name__ == "__main__":
    run_all()
