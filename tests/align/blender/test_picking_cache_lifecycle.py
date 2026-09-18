"""레이어 삭제의 확정·실패 복구 경계에서 실제 면 피킹 캐시 수명을 검증합니다."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


def test_layer_deletion_preserves_or_discards_owned_pick_cache() -> None:
    """동일 OBJ의 독립 BVH를 만들고 삭제 실패 시 보존·성공 시 소유 캐시만 해제합니다."""
    from test_duplicate_sources import _import_copies, _remove_meshes
    from test_embedded_obj import _write_fixture
    from test_project_restore import _remove_owned_project_texts
    from tools.align.constants import MARKER_GEOMETRY_SOURCE_COMPANION
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh
    from tools.align.operators import model_group
    from tools.align.state import RUNTIME
    from tools.align.viewport import picking

    RUNTIME.clear_transient()
    picking.clear_mesh_surface_pick_cache()
    models = []
    try:
        with TemporaryDirectory() as temporary:
            path = _write_fixture(Path(temporary))
            models = _import_copies(RUNTIME, path, count=2)
            first, second = models
            first_tree = picking._mesh_surface_bvh(first.model_id, first)
            companion_tree = picking._mesh_surface_bvh(
                first.model_id, first, MARKER_GEOMETRY_SOURCE_COMPANION,
            )
            second_tree = picking._mesh_surface_bvh(second.model_id, second)
            assert first_tree is not second_tree
            assert companion_tree is not first_tree
            cache_before = dict(picking._MESH_SURFACE_BVH_CACHE)
            assert len(cache_before) == 3
            RUNTIME.model_catalog.select_model(
                first.model_id, has_marker_draft=False, completed_pair_count=0,
            )
            with patch.object(
                model_group, "sync_project_state_if_dirty",
                side_effect=RuntimeError("삭제 저장 실패 검사"),
            ):
                try:
                    model_group.delete_model_from_runtime(first.model_id)
                except RuntimeError as error:
                    assert str(error) == "삭제 저장 실패 검사"
                else:
                    raise AssertionError("삭제 실패가 전달되지 않았습니다.")
            assert tuple(RUNTIME.model_repository.models) == (first.model_id, second.model_id)
            assert picking._MESH_SURFACE_BVH_CACHE.keys() == cache_before.keys()
            assert all(picking._MESH_SURFACE_BVH_CACHE[key] is tree for key, tree in cache_before.items())
            assert picking._mesh_surface_bvh(first.model_id, first) is first_tree
            assert find_embedded_obj_mesh(first.embedded_asset_uuid) is not None

            result = model_group.delete_model_from_runtime(first.model_id)
            assert result.model_id == first.model_id
            assert all(key[0] != first.model_id for key in picking._MESH_SURFACE_BVH_CACHE)
            assert len(picking._MESH_SURFACE_BVH_CACHE) == 1
            assert picking._mesh_surface_bvh(second.model_id, second) is second_tree
            assert RUNTIME.model_repository.models[second.model_id] is second
            assert RUNTIME.model_repository.model_id_for_source_path(path) == second.model_id
            assert find_embedded_obj_mesh(first.embedded_asset_uuid, allow_missing=True) is None
            assert find_embedded_obj_mesh(second.embedded_asset_uuid) is not None
    finally:
        picking.clear_mesh_surface_pick_cache()
        _remove_meshes(models)
        RUNTIME.clear_transient()
        _remove_owned_project_texts()


if __name__ == "__main__":
    for source_path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        sys.path.insert(0, str(source_path))
    test_layer_deletion_preserves_or_discards_owned_pick_cache()
    print("피킹 캐시 수명 검증 통과: 삭제 실패 복구·성공 삭제·다른 모델 보존")
