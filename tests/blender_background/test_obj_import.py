"""공통 OBJ 가져오기의 소유권·문맥 복구·실패 정리를 실제 Blender에서 검증합니다."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.blender_runtime import obj_import


def _snapshot():
    """기존 사용자 데이터를 보호하는 비교용 ID 집합을 반환합니다."""

    return tuple(set(getattr(bpy.data, name)) for name in ("objects", "meshes", "materials", "images", "collections"))


def run_all():
    """실제 기본 가져오기의 확정·취소·부분 실패와 편집 문맥 복원을 확인합니다."""

    active = bpy.context.view_layer.objects.active
    selected = tuple(bpy.context.selected_objects)
    layer = bpy.context.view_layer.active_layer_collection
    existing = bpy.data.materials.new("surface")
    existing_image = bpy.data.images.new("texture.png", width=1, height=1)
    with TemporaryDirectory() as folder:
        root = Path(folder)
        existing_image.filepath_raw = str(root / "texture.png")
        existing_image.file_format = "PNG"
        existing_image.save()
        (root / "model.mtl").write_text("newmtl surface\nKd 1 0 0\nmap_Kd texture.png\n", encoding="utf-8")
        path = root / "model.obj"
        path.write_text("mtllib model.mtl\nv 0 0 0\nv 2 0 0\nv 2 3 0\nv 0 3 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nusemtl surface\nf 1/1 2/2 3/3 4/4\n", encoding="utf-8")
        baseline = _snapshot()
        bpy.ops.object.mode_set(mode="EDIT")
        staging = obj_import.import_obj_to_staging(path)
        assert bpy.context.view_layer.objects.active == active
        assert set(bpy.context.selected_objects) == set(selected)
        assert bpy.context.view_layer.active_layer_collection == layer
        assert active.mode == "EDIT"
        assert len(staging.mesh.polygons) == 1 and staging.mesh.polygons[0].loop_total == 4
        assert tuple(tuple(item.uv) for item in staging.mesh.uv_layers.active.data) == ((0, 0), (1, 0), (1, 1), (0, 1))
        assert staging.mesh.materials[0] != existing
        assert any(node.type == "TEX_IMAGE" and node.image is not None for node in staging.mesh.materials[0].node_tree.nodes)
        staging.discard()
        staging.discard()
        assert _snapshot() == baseline
        assert bpy.data.materials.get(existing.name) == existing
        assert bpy.data.images.get(existing_image.name) == existing_image
        native = bpy.ops.wm.obj_import

        def _partial_failure(**kwargs):
            """기본 가져오기가 자원을 생성한 직후 실패한 상황을 재현합니다."""

            native(**kwargs)
            raise RuntimeError("가져오기 직후 실패 검증")

        with patch.object(obj_import, "bpy", SimpleNamespace(
            data=bpy.data, context=bpy.context,
            ops=SimpleNamespace(object=bpy.ops.object, wm=SimpleNamespace(obj_import=_partial_failure)),
        )):
            try:
                obj_import.import_obj_to_staging(path)
            except obj_import.ObjNativeImportError:
                pass
            else:
                raise AssertionError("부분 실패가 전달되지 않았습니다.")
        assert _snapshot() == baseline
        assert bpy.context.view_layer.objects.active == active and active.mode == "EDIT"
        assert set(bpy.context.selected_objects) == set(selected)
        assert bpy.context.view_layer.active_layer_collection == layer
        staging = obj_import.import_obj_to_staging(path)
        mesh = staging.commit()
        assert mesh is staging.mesh and staging.committed
        assert "owner" not in mesh and "asset_uuid" not in mesh
        for material in staging.materials:
            assert "owner" not in material and "asset_uuid" not in material
        staging.discard()
        assert bpy.data.meshes.get(mesh.name) == mesh
        bpy.data.meshes.remove(mesh)
        staging._remove_unused_support_data()
        assert _snapshot() == baseline
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.data.materials.remove(existing)
    bpy.data.images.remove(existing_image)
    print("공통 OBJ 가져오기 계약: 통과")


if __name__ == "__main__":
    run_all()
