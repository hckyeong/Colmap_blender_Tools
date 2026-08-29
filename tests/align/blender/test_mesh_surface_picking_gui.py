"""실제 Blender VIEW_3D 투영 광선이 메시 면 마커를 반환하는지 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import traceback

import bpy
from bpy_extras import view3d_utils
from mathutils import Quaternion, Vector
import numpy as np


_TEST_CONTEXT: tuple[object, object, object] | None = None


def _add_source_path() -> None:
    """애드온 소스 경로를 GUI 테스트의 Python 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _mesh_model():
    """실제 VIEW_3D 피킹에 사용할 XY 평면 정사각형 메시를 생성합니다."""

    from tools.align.mesh_io.types import TriangleMeshModelData

    vertices = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        dtype=np.float64,
    )
    return TriangleMeshModelData(
        model_id="surface-gui-mesh",
        display_name="surface-gui-mesh",
        source_path=Path.cwd() / "surface-gui-mesh.obj",
        vertices_float64=vertices,
        triangles_uint32=np.asarray(((0, 1, 2), (0, 2, 3)), dtype=np.uint32),
        registration_vertex_indices=np.arange(4, dtype=np.uint32),
        display_vertex_indices=np.arange(4, dtype=np.uint32),
        bounds_min=vertices.min(axis=0),
        bounds_max=vertices.max(axis=0),
    )


def _verify_gui_pick() -> None:
    """뷰 행렬 갱신 다음 tick에 면 중앙 클릭이 MESH_SURFACE로 피킹되는지 확인합니다."""

    from tools.align.constants import MARKER_POINT_KIND_MESH_SURFACE
    from tools.align.state import RUNTIME
    from tools.align.viewport.picking import (
        clear_mesh_surface_pick_cache,
        pick_nearest_point_for_model_ids,
    )

    global _TEST_CONTEXT
    if _TEST_CONTEXT is None:
        raise AssertionError("GUI 면 피킹 테스트 컨텍스트가 비어 있습니다.")
    model, region, region_3d = _TEST_CONTEXT
    expected = Vector((0.3, 0.2, 0.0))
    screen_position = view3d_utils.location_3d_to_region_2d(
        region,
        region_3d,
        expected,
    )
    if screen_position is None:
        raise AssertionError("메시 면 테스트 좌표를 VIEW_3D에 투영하지 못했습니다.")
    result = pick_nearest_point_for_model_ids(
        (model.model_id,),
        region,
        region_3d,
        (float(screen_position.x), float(screen_position.y)),
    )
    if result is None:
        raise AssertionError("메시 면 중앙 피킹 결과가 없습니다.")
    if result.point_kind != MARKER_POINT_KIND_MESH_SURFACE:
        raise AssertionError(f"면 대신 다른 마커 종류가 선택되었습니다: {result.point_kind}")
    np.testing.assert_allclose(result.position, expected, rtol=0.0, atol=1.0e-6)
    clear_mesh_surface_pick_cache()
    RUNTIME.clear_transient()
    print("MESH_SURFACE_PICKING_GUI_TEST_RESULT=PASS", flush=True)


def _finish_gui_test() -> None:
    """타이머 검증 결과를 출력하고 Blender GUI 프로세스를 종료합니다."""

    try:
        _verify_gui_pick()
    except Exception:
        traceback.print_exc()
        print("MESH_SURFACE_PICKING_GUI_TEST_RESULT=FAIL", flush=True)
    finally:
        bpy.ops.wm.quit_blender()


def run_gui_test() -> None:
    """실제 Area·RegionView3D를 설정하고 다음 UI tick의 면 피킹 검증을 예약합니다."""

    from tools.align.constants import GEOMETRY_KIND_TRIANGLE_MESH
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import RUNTIME

    window = bpy.context.window
    if window is None:
        raise AssertionError("Blender GUI 창을 찾을 수 없습니다.")
    area = next((item for item in window.screen.areas if item.type == "VIEW_3D"), None)
    if area is None:
        raise AssertionError("GUI 테스트에 VIEW_3D Area가 없습니다.")
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        raise AssertionError("GUI 테스트에 VIEW_3D WINDOW Region이 없습니다.")

    RUNTIME.clear_transient()
    model = _mesh_model()
    RUNTIME.model_repository.add_model(model, identity_display_transform())
    RUNTIME.model_catalog.add_imported_model(
        model.model_id,
        model.display_name,
        select_after_import=False,
        geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
    )
    region_3d = area.spaces.active.region_3d
    region_3d.view_perspective = "ORTHO"
    region_3d.view_location = Vector((0.5, 0.5, 0.0))
    region_3d.view_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
    region_3d.view_distance = 2.0
    area.tag_redraw()
    global _TEST_CONTEXT
    _TEST_CONTEXT = (model, region, region_3d)
    bpy.app.timers.register(_finish_gui_test, first_interval=0.5)


if __name__ == "__main__":
    _add_source_path()
    run_gui_test()
