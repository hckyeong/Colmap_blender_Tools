"""사용자 애드온 폴더에 설치된 패키지의 메시 면 피킹을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import addon_utils
import bpy
import numpy as np


def _installed_mesh(model_id: str):
    """설치본 피킹 검증에 사용할 한 삼각면 메시를 생성합니다."""

    from tools.align.mesh_io.types import TriangleMeshModelData

    vertices = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        dtype=np.float64,
    )
    return TriangleMeshModelData(
        model_id=model_id,
        display_name=model_id,
        source_path=Path.cwd() / "installed-surface.obj",
        vertices_float64=vertices,
        triangles_uint32=np.asarray(((0, 1, 2),), dtype=np.uint32),
        registration_vertex_indices=np.arange(3, dtype=np.uint32),
        display_vertex_indices=np.arange(3, dtype=np.uint32),
        bounds_min=vertices.min(axis=0),
        bounds_max=vertices.max(axis=0),
    )


def run_test() -> None:
    """설치 경로·schema v3·MESH_SURFACE 광선 교차를 하나의 설치본 계약으로 검증합니다."""

    addon_utils.enable("colmap_align", default_set=False, persistent=False)
    from tools import align as colmap_align
    import colmap_align.viewport.picking as picking
    import colmap_align.viewport.point_data as point_data
    from tools.align.constants import (
        MARKER_POINT_KIND_MESH_SURFACE,
        PROJECT_PERSISTENCE_SCHEMA_VERSION,
    )
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import RuntimeState

    addon_file = Path(colmap_align.__file__).resolve()
    user_addons = (
        Path(bpy.utils.user_resource("SCRIPTS", path="addons", create=False)).resolve()
    )
    if user_addons not in addon_file.parents:
        raise AssertionError(f"사용자 설치본이 아닌 모듈이 로드되었습니다: {addon_file}")
    assert PROJECT_PERSISTENCE_SCHEMA_VERSION == 3

    runtime = RuntimeState()
    model = _installed_mesh("installed-surface")
    runtime.model_repository.add_model(model, identity_display_transform())
    original_runtime = point_data.RUNTIME
    point_data.RUNTIME = runtime
    try:
        view_matrix = np.identity(4, dtype=np.float64)
        view_matrix[2, 3] = -2.0
        result = picking._pick_model_mesh_surface_from_ray(
            model.model_id,
            model,
            SimpleNamespace(width=800, height=600),
            SimpleNamespace(view_matrix=view_matrix, perspective_matrix=None),
            np.asarray((0.2, 0.3, 1.0), dtype=np.float64),
            np.asarray((0.0, 0.0, -1.0), dtype=np.float64),
        )
        assert result is not None
        assert result.point_kind == MARKER_POINT_KIND_MESH_SURFACE
        np.testing.assert_allclose(result.position, (0.2, 0.3, 0.0), rtol=0.0, atol=1.0e-12)
    finally:
        picking.clear_mesh_surface_pick_cache()
        point_data.RUNTIME = original_runtime
        addon_utils.disable("colmap_align", default_set=False)
    print("test_installed_mesh_surface: 통과", flush=True)


if __name__ == "__main__":
    run_test()
