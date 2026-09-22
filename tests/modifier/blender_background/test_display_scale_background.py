"""실제 Blender에서 데이터 배율과 표시 크기의 독립성을 검증합니다."""

import sys
import tempfile
from time import perf_counter
from statistics import mean
from pathlib import Path
from unittest.mock import patch

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools import modifier
from tools.modifier.blender.axis_display import apply_dataset_transform_preview, dataset_transform_roots
from tools.modifier.blender.display_scale import DisplayScaleTargets, RADIUS_NODE
from tools.modifier.blender.edit_workflows import commit_dataset_transform, undo_last_edit, redo_last_edit
from tools.modifier.blender.point_display import sync_point_scale_from_modifiers
from tools.modifier.blender.ownership import RECORD_KIND_KEY
from tests.modifier.fixture_utils import make_legacy_text_multi_point


def check_display(scale):
    """월드 카메라 크기와 점 반지름의 보정 및 공개 설정 보존을 검사합니다."""
    bpy.context.view_layer.update()
    public_input = getattr(points.modifiers[0].properties.inputs, node_group["colmap_point_scale_socket"])
    assert all(abs(value - 1.0) < 2e-5 for value in camera.matrix_world.to_scale())
    assert abs(camera.data.display_size - settings.camera_frustum_size) < 1e-6
    assert abs(public_input.value - settings.point_size) < 1e-6
    assert abs(node_group.nodes[RADIUS_NODE].inputs[1].default_value * scale - 1.0) < 2e-6
    evaluated = points.evaluated_get(bpy.context.evaluated_depsgraph_get())
    geometry = evaluated.evaluated_geometry()
    cloud = geometry.pointcloud
    assert cloud is not None
    assert all(abs(point.radius * scale - settings.point_size) < 2e-6 for point in cloud.points)
    assert tuple(tuple(v.co) for v in points.data.vertices) == coordinates
    assert (camera.matrix_basis.translation - camera_local).length < 1e-6
    assert (camera.matrix_world.translation - (roots[0].matrix_world @ camera_local)).length < 2e-5


modifier.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        dataset = Path(directory) / "dataset"
        make_legacy_text_multi_point(dataset)
        scene = bpy.context.scene
        settings = scene.colmap_import
        settings.dataset_root, settings.input_format = str(dataset), "TEXT"
        assert bpy.ops.colmap.import_model() == {"FINISHED"}
        points = next(o for o in scene.objects if o.get(RECORD_KIND_KEY) == "POINT_MESH")
        camera = next(o for o in scene.objects if o.get(RECORD_KIND_KEY) == "CAMERA")
        coordinates = tuple(tuple(v.co) for v in points.data.vertices)
        camera_local = camera.matrix_basis.translation.copy()
        node_group = points.modifiers[0].node_group
        public_input = getattr(points.modifiers[0].properties.inputs, node_group["colmap_point_scale_socket"])
        roots = dataset_transform_roots(scene)
        targets = DisplayScaleTargets(scene, roots)
        for scale in (0.1, 1.0, 10.0, 100.0):
            matrix = Matrix.Translation((2.0, 3.0, 4.0)) @ Matrix.Rotation(0.4, 4, "Z") @ Matrix.Scale(scale, 4)
            apply_dataset_transform_preview(scene, matrix, roots=roots, display_targets=targets)
            check_display(scale)
            with patch("tools.modifier.blender.display_scale.compensate_object", side_effect=AssertionError("동일 배율 재계산")):
                moved = matrix.copy()
                moved.translation += Vector((1, 2, 3))
                apply_dataset_transform_preview(scene, moved, roots=roots, display_targets=targets)
        apply_dataset_transform_preview(scene)
        check_display(1)
        commit_dataset_transform(bpy.context, Matrix.Scale(10.0, 4))
        check_display(10)
        undo_last_edit(bpy.context)
        check_display(1)
        redo_last_edit(bpy.context)
        check_display(10)
        settings.point_size = 0.12
        check_display(10)
        public_input.value = 0.17
        sync_point_scale_from_modifiers(scene, None)
        assert abs(settings.point_size - 0.17) < 1e-6
        check_display(10)
        node_group.nodes.remove(node_group.nodes[RADIUS_NODE])
        node_group.links.new(node_group.nodes["COLMAP_Group_Input"].outputs["Point Scale"],
                             node_group.nodes["COLMAP_Mesh_To_Points"].inputs["Radius"])
        socket_id = node_group["colmap_point_scale_socket"]
        apply_dataset_transform_preview(scene)
        assert node_group["colmap_point_scale_socket"] == socket_id
        check_display(10)
        before_nodes = len(node_group.nodes)
        apply_dataset_transform_preview(scene)
        assert len(node_group.nodes) == before_nodes
        copies = []
        for count in (100, 1000):
            while len(copies) < count - 1:
                duplicate = camera.copy()
                scene.collection.objects.link(duplicate)
                copies.append(duplicate)
            targets = DisplayScaleTargets(scene, roots)
            apply_times, evaluation_times = [], []
            for scale in (1.0, 10.0, 0.1, 100.0, 1.0, 10.0):
                started = perf_counter()
                apply_dataset_transform_preview(scene, Matrix.Scale(scale, 4), roots=roots,
                                                display_targets=targets)
                applied = perf_counter()
                bpy.context.view_layer.update()
                finished = perf_counter()
                apply_times.append((applied - started) * 1000)
                evaluation_times.append((finished - applied) * 1000)
            print(f"카메라 {count}개: 보정 평균 {mean(apply_times):.3f}밀리초, 평가 평균 {mean(evaluation_times):.3f}밀리초")
finally:
    modifier.unregister()

print("표시 크기 독립성과 구형 노드 이전 검증 통과")
