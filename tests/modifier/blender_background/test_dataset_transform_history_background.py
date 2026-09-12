"""실제 Blender에서 변환 이력·표시·원자 복구와 전역 실행 취소를 검증합니다."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy
import bmesh
from mathutils import Matrix

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier
from tools.modifier.blender.axis_display import display_axis_matrix
from tools.modifier.blender.edit_workflows import undo_last_edit, redo_last_edit, reset_edits, commit_dataset_transform
from tools.modifier.blender.modifier_runtime_domain import domain_for_scene
from tools.modifier.blender.runtime_state import ensure_runtime
from tools.modifier.blender.ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY
from tools.modifier.blender.undo_domain_coordinator import (
    current_undo_protection_snapshot, process_pending_undo_reconcile,
    schedule_undo_domain_reconcile, undo_domain_reconcile_pending,
)
from tools.modifier.core.dataset_transform import IDENTITY_TRANSFORM
from tests.modifier.fixture_utils import make_legacy_text_multi_point

TRANSFORM = ((1.0, 0.0, 0.0, 4.0), (0.0, 1.0, 0.0, -2.0),
             (0.0, 0.0, 1.0, 3.0), (0.0, 0.0, 0.0, 1.0))


def check(value, message):
    """검증 조건이 거짓이면 한국어 실패 메시지를 반환합니다."""
    if not value:
        raise AssertionError(message)


def managed(kind):
    """현재 장면에 연결된 Modifier 소유 표시 객체를 반환합니다."""
    return next(obj for obj in bpy.context.scene.objects
                if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == kind)


def view_override():
    """배경 모드의 기존 삼차원 영역을 연산자 문맥으로 제공합니다."""
    window = bpy.context.window
    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "WINDOW")
    return bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region)


def check_root(transform):
    """표시 루트가 표시축과 확정 변환을 순서대로 합성했는지 확인합니다."""
    settings = bpy.context.scene.colmap_import
    expected = display_axis_matrix(settings.axis_forward, settings.axis_up) @ Matrix(transform)
    actual = managed("AXIS_ROOT").matrix_world
    check(max(abs(actual[row][col] - expected[row][col]) for row in range(4) for col in range(4)) < 1e-6,
          "표시 루트가 표시축과 데이터셋 변환의 곱과 다릅니다.")


def remove_first_point():
    """실제 점 선택 모드와 제거 연산자를 통해 한 점을 삭제합니다."""
    with view_override():
        check(bpy.ops.colmap.toggle_edit_point_mode() == {"FINISHED"}, "점 편집 진입 실패")
    obj = managed("POINT_MESH")
    mesh = bmesh.from_edit_mesh(obj.data)
    mesh.verts.ensure_lookup_table()
    for vertex in mesh.verts:
        vertex.select = False
    mesh.verts[0].select = True
    bmesh.update_edit_mesh(obj.data)
    with view_override():
        check(bpy.ops.colmap.remove() == {"FINISHED"}, "점 삭제 실패")


def reconcile():
    """전역 실행 취소가 예약한 도메인 표시 복원을 완료합니다."""
    if not undo_domain_reconcile_pending():
        schedule_undo_domain_reconcile(current_undo_protection_snapshot())
    for _index in range(12):
        process_pending_undo_reconcile()
        if not undo_domain_reconcile_pending():
            return
    raise AssertionError("전역 실행 취소 재동기화가 완료되지 않았습니다.")


modifier.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        make_legacy_text_multi_point(root)
        settings = bpy.context.scene.colmap_import
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        check(bpy.ops.colmap.import_model() == {"FINISHED"}, settings.status)
        remove_first_point()
        before = ensure_runtime(bpy.context.scene)
        obj = managed("POINT_MESH")
        mesh_pointer = obj.data.as_pointer()
        coordinates = tuple(tuple(vertex.co) for vertex in obj.data.vertices)
        changed = commit_dataset_transform(bpy.context, TRANSFORM)
        check(changed.current_reconstruction is before.current_reconstruction, "변환이 기준 레코드를 복사했습니다.")
        check(managed("POINT_MESH").data.as_pointer() == mesh_pointer, "변환이 점 메시를 재생성했습니다.")
        check(tuple(tuple(vertex.co) for vertex in managed("POINT_MESH").data.vertices) == coordinates,
              "변환이 점 메시의 원본 좌표를 변경했습니다.")
        check(changed.edit_revision == 2 and domain_for_scene(bpy.context.scene).journal.undo_count == 2,
              "변환 확정이 이력 한 건으로 기록되지 않았습니다.")
        check_root(TRANSFORM)
        check(commit_dataset_transform(bpy.context, TRANSFORM) is changed, "무변경 변환이 새 이력을 만들었습니다.")
        remove_first_point()
        check(len(ensure_runtime(bpy.context.scene).current_reconstruction.points3d) == 1, "두 번째 삭제 실패")
        undo_last_edit(bpy.context)
        check_root(TRANSFORM)
        undo_last_edit(bpy.context)
        check_root(IDENTITY_TRANSFORM)
        redo_last_edit(bpy.context)
        redo_last_edit(bpy.context)
        reset_edits(bpy.context)
        check_root(IDENTITY_TRANSFORM)
        check(len(ensure_runtime(bpy.context.scene).current_reconstruction.points3d) == 3, "초기화가 점을 복원하지 못했습니다.")
        undo_last_edit(bpy.context)
        check_root(TRANSFORM)
        check(len(ensure_runtime(bpy.context.scene).current_reconstruction.points3d) == 1, "초기화 취소가 혼합 이력을 복원하지 못했습니다.")
        redo_last_edit(bpy.context)
        before = ensure_runtime(bpy.context.scene)
        domain = domain_for_scene(bpy.context.scene)
        checkpoint = domain.journal.checkpoint()
        from tools.modifier.blender import axis_display
        original_apply = axis_display.apply_dataset_transform_preview
        calls = [0]

        def fail_once(scene, matrix=None):
            """표시 갱신 첫 호출에만 실패를 주입하고 복구 호출은 허용합니다."""
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("검증용 표시 실패")
            return original_apply(scene, matrix)

        with patch.object(axis_display, "apply_dataset_transform_preview", side_effect=fail_once):
            try:
                commit_dataset_transform(bpy.context, TRANSFORM)
            except RuntimeError as exc:
                check("검증용 표시 실패" in str(exc), "예상과 다른 표시 실패")
            else:
                raise AssertionError("주입한 표시 실패가 반환되지 않았습니다.")
        check(ensure_runtime(bpy.context.scene) is before, "표시 실패가 권위 상태를 변경했습니다.")
        check(domain.journal.checkpoint() == checkpoint, "표시 실패가 이력 또는 재실행 분기를 변경했습니다.")
        check_root(IDENTITY_TRANSFORM)
        with view_override():
            bpy.ops.ed.undo_push(message="변환 전 기준")
        committed = commit_dataset_transform(bpy.context, TRANSFORM)
        with view_override():
            bpy.ops.ed.undo_push(message="변환 후 표시")
            check(bpy.ops.ed.undo() == {"FINISHED"}, "Blender 전역 실행 취소 실패")
        reconcile()
        check(ensure_runtime(bpy.context.scene) is committed, "전역 실행 취소가 도메인 권위를 변경했습니다.")
        check_root(TRANSFORM)
finally:
    modifier.unregister()

print("데이터셋 변환 이력 Blender 배경 검증 통과")
