"""화면 마커의 정밀 광선·오류 경계·준비 배치 수명 계약을 GPU 없이 검증합니다."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.align.viewport import picking, renderer, screen_pick
from tools.align.model_repository import ModelRepositoryError


def test_precise_ray_reprojects_click():
    """먼 원근 화면과 직교 화면의 광선이 배정밀도로 원래 클릭에 재투영되는지 검사합니다."""
    region = SimpleNamespace(width=1921, height=1081)
    perspective = np.asarray(((1.7, 0, 0, 0), (0, 2.1, 0, 0), (0, 0, -1.000002, -.02000002), (0, 0, -1, 0)), dtype=np.float64)
    view = np.eye(4)
    view[:3, 3] = (123.456789, -987.654321, -1234.56789)
    for projection in (perspective @ view, np.diag((.01, .01, -.001, 1)) @ view):
        for mouse in ((0, 0), (region.width - .01, region.height - .01), (374.1234, 891.2345)):
            origin, direction = picking._screen_view_ray(region, SimpleNamespace(perspective_matrix=projection), mouse)
            point = origin + direction * 1000
            clip = projection @ np.append(point, 1)
            screen = (clip[:2] / clip[3] + 1) * (region.width / 2, region.height / 2)
            np.testing.assert_allclose(screen, mouse, atol=1e-7, rtol=0)


def test_depth_range_and_patch_center():
    """선형 깊이가 원본 범위에서 앞뒤 순서를 보존하고 클릭이 중앙 픽셀에 오는지 검사합니다."""
    region = SimpleNamespace(width=2001, height=1001)
    projection = np.eye(4)
    mouse = (123.456, 789.123)
    crop = screen_pick._patch_matrix(region, projection, mouse)
    center = crop @ np.asarray((2 * mouse[0] / region.width - 1, 2 * mouse[1] / region.height - 1, 0, 1))
    np.testing.assert_allclose(center[:2], 0, atol=1e-9)
    view = np.eye(4)
    view[2, 3] = -100000
    plane = screen_pick._linear_depth_plane(view, np.eye(4), ((-1, -2, -3), (1, 2, 3)))
    near = plane @ np.asarray((0, 0, 3, 1))
    far = plane @ np.asarray((0, 0, -3, 1))
    assert 0 < near < far < 1
    assert far - near > .9


def test_gpu_failure_does_not_build_bvh_or_snap_vertex():
    """화면 선택 실패가 BVH 생성이나 임의 정점 선택으로 바뀌지 않는지 검사합니다."""
    from test_companion_marker_alignment import _runtime, _display_runtime
    runtime, *_ = _runtime()
    before = runtime.marker_selection.snapshot()
    with (
        _display_runtime(runtime),
        patch.object(picking, "_screen_view_ray", return_value=(np.zeros(3), np.asarray((0, 0, -1.0)))),
        patch.object(picking, "_ray_intersects_bounds", return_value=True),
        patch.object(renderer, "prepared_mesh_pick_batch", side_effect=RuntimeError("검사용 GPU 오류")),
        patch.object(picking, "_mesh_surface_bvh") as bvh,
        patch.object(picking, "_pick_model_nearest_point") as vertex,
    ):
        try:
            picking.pick_nearest_point("MOVING", SimpleNamespace(), SimpleNamespace(), (0, 0))
        except ModelRepositoryError as error:
            assert "검사용 GPU 오류" in str(error)
        else:
            raise AssertionError("GPU 오류가 전달되지 않았습니다.")
        bvh.assert_not_called()
        vertex.assert_not_called()
    assert runtime.marker_selection.snapshot() == before


def test_prepared_batch_rejects_stale_geometry():
    """같은 모델 이름·개수라도 교체된 원본의 낡은 GPU ID 배치를 거절합니다."""
    from test_mesh_surface_picking import _mesh_model
    model = _mesh_model()
    batch = object()
    cached = renderer._MeshSurfaceBatch(batch, (), pick_batch=batch, source_token=id(model))
    with patch.dict(renderer.RUNTIME.gpu_batches, {renderer._mesh_batch_key(model): cached}, clear=True):
        assert renderer.prepared_mesh_pick_batch(model.model_id, model) is batch
        try:
            renderer.prepared_mesh_pick_batch(model.model_id, replace(model))
        except RuntimeError:
            pass
        else:
            raise AssertionError("교체된 원본에 낡은 ID가 사용되었습니다.")


def test_modal_gpu_failure_keeps_draft():
    """실제 클릭 처리기가 GPU 오류를 안내하고 마커 드래프트·Undo를 바꾸지 않는지 검사합니다."""
    from test_companion_marker_alignment import _runtime
    from tools.align.operators import marker_pick
    runtime, *_ = _runtime()
    assert runtime.marker_selection.begin_create_draft(runtime=runtime)
    before = runtime.marker_selection.snapshot()
    errors = []
    operator = SimpleNamespace(_apply_pending_command=lambda context: None,
                               _report_input_rejection=lambda context, reason: errors.append(reason))
    region = SimpleNamespace(x=0, y=0)
    area = SimpleNamespace(spaces=SimpleNamespace(active=SimpleNamespace(region_3d=object())))
    event = SimpleNamespace(type="LEFTMOUSE", value="PRESS", mouse_x=50, mouse_y=50)
    with (
        patch.object(marker_pick, "RUNTIME", runtime),
        patch.object(marker_pick, "is_session_active_for_window", return_value=True),
        patch.object(marker_pick, "refresh_registry", return_value=True),
        patch.object(marker_pick, "find_viewport_under_mouse", return_value=(area, region, "MOVING")),
        patch.object(marker_pick, "pick_nearest_point", side_effect=ModelRepositoryError("검사용 GPU 오류")),
    ):
        result = marker_pick.COLMAPALIGN_OT_marker_pick_modal.modal(operator, SimpleNamespace(window=object()), event)
    assert result == {"RUNNING_MODAL"} and errors == ["검사용 GPU 오류"]
    assert runtime.marker_selection.snapshot() == before and runtime.undo_journal.draft_undo_count == 0


def test_invalid_screen_ray_is_rejected():
    """잘못된 화면 범위와 특이 투영을 선택 실패로 명확하게 거절합니다."""
    for width, matrix, mouse in ((0, np.eye(4), (0, 0)), (100, np.zeros((4, 4)), (50, 50)), (100, np.eye(4), (-1, 50))):
        try:
            picking._screen_view_ray(SimpleNamespace(width=width, height=100), SimpleNamespace(perspective_matrix=matrix), mouse)
        except ValueError:
            pass
        else:
            raise AssertionError("유효하지 않은 화면 광선이 허용되었습니다.")


def run_all():
    """새 마커 선택의 배정밀도 계산과 실패 계약 검사를 실행합니다."""
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("통과:", name, flush=True)


if __name__ == "__main__":
    run_all()
