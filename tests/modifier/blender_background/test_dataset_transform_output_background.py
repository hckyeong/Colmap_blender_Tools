"""Blender 저장 재실행과 출력 재기준화 뒤 표시·Painter 전달을 검증합니다."""

from pathlib import Path
import sys
import tempfile

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools import modifier
from tools.modifier.blender.edit_workflows import commit_dataset_transform
from tools.modifier.blender.modifier_runtime_domain import domain_for_scene
from tools.modifier.blender.state_handlers import process_pending_load_restore
from tools.modifier.blender.projection_snapshot_provider import build_projection_snapshot
from tools.modifier.blender.ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY, DATASET_UUID_KEY
from tools.modifier.core.dataset_transform import IDENTITY_TRANSFORM
from tests.modifier.fixture_utils import make_legacy_text_multi_point


def point_object():
    """현재 데이터셋에 속한 표시 점 오브젝트 하나를 조회합니다."""
    return next(obj for obj in bpy.data.objects if obj.get(OWNER_KEY) == OWNER_TOKEN
                and obj.get(RECORD_KIND_KEY) == "POINT_MESH"
                and obj.get(DATASET_UUID_KEY) == bpy.context.scene.colmap_import.dataset_uuid)


def world_positions():
    """평가가 완료된 부모 변환을 포함해 화면상의 점 위치를 기록합니다."""
    bpy.context.view_layer.update()
    obj = point_object()
    return [obj.matrix_world @ v.co for v in obj.data.vertices]


def assert_positions(expected):
    """저장·출력 전후의 화면 점 위치가 같은지 확인합니다."""
    actual = world_positions()
    assert len(actual) == len(expected)
    for a, b in zip(actual, expected):
        assert (a - b).length < 1.e-5, (a, b)


modifier.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_legacy_text_multi_point(root)
        settings = bpy.context.scene.colmap_import
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        assert bpy.ops.colmap.import_model() == {"FINISHED"}
        matrix = ((0., -2., 0., 4.), (2., 0., 0., 5.), (0., 0., 2., 6.), (0., 0., 0., 1.))
        commit_dataset_transform(bpy.context, matrix)
        expected = world_positions()
        before_snapshot = build_projection_snapshot(bpy.context)
        blend = root / "transform.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        for _ in range(1000):
            if process_pending_load_restore() is None:
                break
        settings = bpy.context.scene.colmap_import
        assert not settings.domain_restore_error, settings.domain_restore_error
        restored = domain_for_scene(bpy.context.scene, required=True)
        assert restored.dataset_state.dataset_transform == matrix
        assert restored.journal.undo_count == 1
        assert_positions(expected)

        settings.export_backup_enabled = False
        assert bpy.ops.colmap.export("EXEC_DEFAULT") == {"FINISHED"}, settings.export_status
        exported = domain_for_scene(bpy.context.scene, required=True)
        assert exported.dataset_state.dataset_transform == IDENTITY_TRANSFORM
        assert exported.journal.undo_count == 0
        assert_positions(expected)
        after_snapshot = build_projection_snapshot(bpy.context)
        assert before_snapshot.points3d == after_snapshot.points3d
        assert before_snapshot.images == after_snapshot.images
        assert before_snapshot.cameras == after_snapshot.cameras
        print("Modifier 변환 저장·복원·출력·Painter 전달 검증 통과")
finally:
    modifier.unregister()
