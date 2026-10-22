"""실제 Blender에서 Painter 독립 OBJ 가져오기와 실패 자원 정리를 검증합니다."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import projection_paint as addon
from tools.projection_paint.blender import model_import, dataset_view_layer
from tools.projection_paint.blender.ownership import is_owned, DATASET_UUID_KEY, MANIFEST_KEY
from tools.projection_paint.blender.camera_import import CameraImportJob
from shared.blender_runtime.scene_marker_session import acquire_scene_marker_session
from test_original_layer import _prepare_backend
from test_camera_import_atomicity import _request


def _ids():
    """모든 관련 Blender 자원 집합을 실패 전후 비교합니다."""

    return tuple(set(getattr(bpy.data, name)) for name in ("objects", "meshes", "materials", "images", "collections"))


def _paint_state(settings):
    """기존 페인트 대상과 레이어 순서·이미지 픽셀을 보관합니다."""

    return settings.paint_target, tuple((layer.as_pointer(), layer.name, layer.image, tuple(layer.image.pixels) if layer.image else ()) for layer in settings.layers)


def _must_fail(path):
    """가져오기 실패가 전달되고 기존 자원이 그대로 남는지 검증합니다."""

    before = _ids()
    try:
        model_import.import_obj_model(bpy.context, path)
    except (OSError, RuntimeError, ValueError):
        pass
    else:
        raise AssertionError("실패가 전달되지 않았습니다.")
    assert _ids() == before, "실패 후 생성 자원이 남거나 기존 자원이 변경되었습니다."


def run_all():
    """작은 실제 파일로 독립 가져오기·원본 보존·원자적 취소를 확인합니다."""

    addon.register()
    scene = bpy.context.scene
    settings = scene.projection_paint
    target, setup = _prepare_backend("기존페인트대상")
    settings.paint_target = target
    paint_before = _paint_state(settings)
    selected = set(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    layer = bpy.context.view_layer.active_layer_collection
    ordinary = bpy.context.view_layer
    assert not scene.get(MANIFEST_KEY)
    with TemporaryDirectory() as folder:
        root = Path(folder)
        texture = bpy.data.images.new("기존사용자텍스처", width=2, height=2)
        texture.filepath_raw = str(root / "texture.png")
        texture.file_format = "PNG"
        texture.save()
        # 같은 파일을 사용하는 기존 사용자 이미지를 그대로 보호해야 합니다.
        material = bpy.data.materials.new("surface")
        (root / "model.mtl").write_text("newmtl surface\nKd 1 0 0\nmap_Kd texture.png\n", encoding="utf-8")
        path = root / "model.obj"
        path.write_text("mtllib model.mtl\nv 0 0 0\nv 2 0 0\nv 2 3 0\nv 0 3 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nusemtl surface\nf 1/1 2/2 3/3 4/4\n", encoding="utf-8")
        settings.model_axis_preset = "BLENDER"
        first = model_import.import_obj_model(bpy.context, path)
        second = model_import.import_obj_model(bpy.context, path)
        assert first.data != second.data and first.parent != second.parent
        assert first[DATASET_UUID_KEY] != second[DATASET_UUID_KEY]
        for obj in (first, second):
            asset = obj[DATASET_UUID_KEY]
            assert is_owned(obj, kind="MODEL_OBJECT", dataset_uuid=asset)
            assert is_owned(obj.parent, kind="MODEL_AXIS_ROOT", dataset_uuid=asset)
            assert is_owned(obj.data, kind="MODEL_MESH", dataset_uuid=asset)
            assert obj.data.polygons[0].loop_total == 4 and len(obj.data.polygons) == 1
            assert tuple(tuple(v.co) for v in obj.data.vertices) == ((0, 0, 0), (2, 0, 0), (2, 3, 0), (0, 3, 0))
            assert tuple(tuple(v.uv) for v in obj.data.uv_layers.active.data) == ((0, 0), (1, 0), (1, 1), (0, 1))
            assert obj.data.materials[0] != material
            assert any(node.type == "TEX_IMAGE" and node.image for node in obj.data.materials[0].node_tree.nodes)
            collection = obj.users_collection[0]
            assert is_owned(collection, kind="MODEL_COLLECTION", dataset_uuid=asset)
            assert dataset_view_layer.layer_collection_for_collection(ordinary.layer_collection, collection).exclude
            pp_layer = dataset_view_layer.ensure_projection_dataset_view_layer(scene)
            assert not dataset_view_layer.layer_collection_for_collection(pp_layer.layer_collection, collection).exclude
        assert _paint_state(settings) == paint_before
        assert set(bpy.context.selected_objects) == selected
        assert bpy.context.view_layer.objects.active == active
        assert bpy.context.view_layer.active_layer_collection == layer
        assert bpy.data.images.get(texture.name) == texture and not is_owned(texture)
        assert bpy.data.materials.get(material.name) == material and not is_owned(material)
        with patch.object(model_import, "_obj_file_identity", side_effect=((1, 2, 3, 4), (2, 3, 4, 5))):
            _must_fail(path)
        with patch.object(dataset_view_layer, "synchronize_projection_dataset_visibility", side_effect=RuntimeError("가시성 확정 실패 검증")):
            _must_fail(path)
        with patch("shared.blender_runtime.obj_import.NativeObjStaging.commit", side_effect=RuntimeError("최종 소유권 확정 실패 검증")):
            _must_fail(path)
        invalid = root / "invalid.obj"
        invalid.write_text("불완전한 OBJ\n", encoding="utf-8")
        _must_fail(invalid)
        settings.model_axis_preset = "CUSTOM"
        settings.model_forward = "Y"
        settings.model_up = "Y"
        _must_fail(path)
        settings.model_axis_preset = "BLENDER"
        acquire_scene_marker_session(scene, "projection_paint")
        CameraImportJob(scene, _request("obj-preservation", revision=1)).run_to_completion()
        CameraImportJob(scene, _request("obj-preservation", revision=2)).run_to_completion()
        assert bpy.data.objects.get(first.name) == first and bpy.data.objects.get(second.name) == second
        assert _paint_state(settings) == paint_before
    addon.unregister()
    print("Painter 독립 OBJ 가져오기 계약: 통과")


if __name__ == "__main__":
    run_all()
