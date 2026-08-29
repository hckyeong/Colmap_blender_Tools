from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path

import bmesh
import bpy


PACKAGE_PARENT = Path(__file__).resolve().parents[3]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tools import modifier as blender_colmap
from tools.modifier.blender import camera_review_timeline, edit_workflows
from tools.modifier.blender import point_edit_session
from tools.modifier.blender.camera_review_session import ensure_review_session
from tools.modifier.blender.delete_guard import _KEYMAP_ITEMS
from tools.modifier.blender.operators_point_edit_mode import (
    _SESSIONS as POINT_EDIT_SESSIONS,
    _restore_pending_point_meshes,
    normalize_external_mode_change,
)
from tools.modifier.blender.operators_point_selection import COLMAP_OT_remove
from tools.modifier.blender.ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY
from tools.modifier.blender.runtime_state import clear_runtime, ensure_runtime
from tools.modifier.blender.selection import (
    collect_selected_point_ids,
    valid_snapshot,
)
from tools.modifier.blender.state_handlers import (
    invalidate_loaded_state,
    reconcile_after_undo_redo,
)
from tests.modifier.fixture_utils import (
    make_legacy_text,
    make_legacy_text_multi_point,
    make_modern_text,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def managed_objects(kind):
    return [
        obj
        for obj in bpy.data.objects
        if obj.get(OWNER_KEY) == OWNER_TOKEN
        and obj.get(RECORD_KIND_KEY) == kind
        and obj.users_collection
    ]


def toggle_point_mode():
    result = bpy.ops.colmap.toggle_edit_point_mode()
    assert_true(result == {"FINISHED"}, bpy.context.scene.colmap_import.point_selection_status)


def toggle_camera_mode():
    result = bpy.ops.colmap.toggle_edit_camera_mode()
    assert_true(result == {"FINISHED"}, bpy.context.scene.colmap_import.point_selection_status)


def select_all_point_vertices():
    point_object = managed_objects("POINT_MESH")[0]
    assert_true(point_object.mode == "EDIT", "Point Object가 Edit Mode가 아닙니다.")
    bm = bmesh.from_edit_mesh(point_object.data)
    for vertex in bm.verts:
        vertex.select = True
    bmesh.update_edit_mesh(point_object.data)
    return point_object


def select_point_vertices(*indexes):
    point_object = managed_objects("POINT_MESH")[0]
    assert_true(point_object.mode == "EDIT", "Point Object가 Edit Mode가 아닙니다.")
    bm = bmesh.from_edit_mesh(point_object.data)
    bm.verts.ensure_lookup_table()
    bm.select_history.clear()
    for vertex in bm.verts:
        vertex.select = False
    for index in indexes:
        bm.verts[index].select = True
    if indexes:
        bm.select_history.add(bm.verts[indexes[-1]])
    bmesh.update_edit_mesh(point_object.data)
    return point_object


def assert_new_point_session_is_clear(point_object, settings):
    bm = bmesh.from_edit_mesh(point_object.data)
    assert_true(
        not any(vertex.select for vertex in bm.verts),
        "재진입 직후 Edit BMesh에 이전 Point 선택이 남았습니다.",
    )
    assert_true(
        not any(vertex.select for vertex in point_object.data.vertices),
        "재진입 직후 원본 Mesh에 이전 Point 선택이 남았습니다.",
    )
    assert_true(
        len(bm.select_history) == 0,
        "재진입 직후 BMesh 선택 이력이 남았습니다.",
    )
    assert_true(
        settings.selected_point_count == 0
        and settings.observing_camera_count == 0,
        "재진입 직후 UI 선택 수가 초기화되지 않았습니다.",
    )
    assert_true(
        settings.remove_target == "POINTS",
        "재진입 직후 Remove 대상이 Point로 설정되지 않았습니다.",
    )


def point_display_signature(point_object):
    mesh = point_object.data
    modifier = point_display_modifier(point_object)
    node_group = modifier.node_group
    point_scale_socket = node_group["colmap_point_scale_socket"]
    point_scale = getattr(
        modifier.properties.inputs,
        point_scale_socket,
    ).value
    return (
        point_object.as_pointer(),
        mesh.as_pointer(),
        tuple(tuple(vertex.co) for vertex in mesh.vertices),
        tuple(
            int(item.value)
            for item in mesh.attributes["colmap_source_index"].data
        ),
        tuple(
            int(item.value)
            for item in mesh.attributes["colmap_point3d_id_low"].data
        ),
        tuple(
            int(item.value)
            for item in mesh.attributes["colmap_point3d_id_high"].data
        ),
        tuple(
            tuple(round(channel, 8) for channel in item.color)
            for item in mesh.color_attributes["colmap_color"].data
        ),
        modifier.as_pointer(),
        node_group.as_pointer(),
        float(point_scale),
    )


def point_display_modifier(point_object):
    modifiers = [
        modifier
        for modifier in point_object.modifiers
        if modifier.type == "NODES"
        and modifier.node_group is not None
        and modifier.node_group.get(RECORD_KIND_KEY) == "POINT_DISPLAY_NODES"
    ]
    assert_true(
        len(modifiers) == 1,
        "관리 Point 표시 Modifier가 정확히 하나가 아닙니다.",
    )
    return modifiers[0]


def assert_point_display_edit_visibility(point_object, expected):
    assert_true(
        point_display_modifier(point_object).show_in_editmode is expected,
        (
            "Edit Point Mode의 Point 표시 Modifier 상태가 잘못되었습니다. "
            f"기대값={expected}"
        ),
    )


def select_observing_cameras():
    result = bpy.ops.colmap.select_observing_cameras()
    assert_true(result == {"FINISHED"}, bpy.context.scene.colmap_import.point_selection_status)


blender_colmap.register()
try:
    scene = bpy.context.scene
    settings = scene.colmap_import
    assert_true(
        bpy.app.handlers.load_post.count(invalidate_loaded_state) == 1,
        "Load Runtime 무효화 handler가 없거나 중복 등록되었습니다.",
    )
    for handlers in (
        bpy.app.handlers.undo_post,
        bpy.app.handlers.redo_post,
    ):
        assert_true(
            handlers.count(reconcile_after_undo_redo) == 1,
            "Undo/Redo 재연결 handler가 없거나 중복 등록되었습니다.",
        )
    assert_true(
        bpy.app.handlers.depsgraph_update_post.count(normalize_external_mode_change) == 1,
        "외부 Point Mode 종료 handler가 없거나 중복 등록되었습니다.",
    )
    assert_true(
        len(_KEYMAP_ITEMS) == 4
        and {item.type for _, item in _KEYMAP_ITEMS} == {"X", "DEL"},
        "Point·Camera 모드용 X/Delete 차단 키맵이 올바르게 등록되지 않았습니다.",
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        multi_root = root / "multi_point_dataset"
        make_legacy_text_multi_point(multi_root)
        settings.dataset_root = str(multi_root)
        settings.input_format = "TEXT"
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        multi_point_object = managed_objects("POINT_MESH")[0]
        assert_true(
            len(multi_point_object.data.vertices) == 3,
            "다중 Point 회귀 fixture가 3개 정점을 만들지 않았습니다.",
        )
        multi_point_display_before = point_display_signature(multi_point_object)

        # Point Mode에서는 평가된 Point Cloud를 숨기고 원본 정점만 선택색으로 표시합니다.
        assert_point_display_edit_visibility(multi_point_object, True)
        toggle_point_mode()
        assert_point_display_edit_visibility(multi_point_object, False)
        select_point_vertices(1)
        toggle_point_mode()
        assert_point_display_edit_visibility(multi_point_object, True)
        snapshot = valid_snapshot(bpy.context, ensure_runtime(scene))
        assert_true(
            snapshot is not None
            and snapshot.selected_point3d_ids == (4294967302,),
            "버튼 종료가 부분 Point 선택 Snapshot을 정확히 보존하지 않았습니다.",
        )
        assert_true(
            not any(vertex.select for vertex in multi_point_object.data.vertices),
            "Edit Point Mode 종료 직후 원본 Mesh에 Point 선택이 남았습니다.",
        )
        toggle_point_mode()
        assert_new_point_session_is_clear(multi_point_object, settings)
        select_point_vertices(2)
        assert_true(
            collect_selected_point_ids(bpy.context, ensure_runtime(scene))
            == (4294967303,),
            "재진입 뒤 새 Point 선택에 이전 Point가 섞였습니다.",
        )
        toggle_point_mode()

        # 외부 Tab/Mode 종료 경로 뒤 재진입도 세 선택 상태를 모두 초기화합니다.
        toggle_point_mode()
        select_point_vertices(0)
        bpy.ops.object.mode_set(mode="OBJECT")
        normalize_external_mode_change(scene, None)
        assert_point_display_edit_visibility(multi_point_object, True)
        assert_true(
            settings.selected_point_count == 1,
            "외부 종료가 부분 Point 선택을 보존하지 않았습니다.",
        )
        toggle_point_mode()
        assert_new_point_session_is_clear(multi_point_object, settings)
        toggle_point_mode()

        # 관측 Camera 전환을 거친 뒤에도 다음 Point 세션은 빈 선택으로 시작합니다.
        toggle_point_mode()
        select_point_vertices(2)
        select_observing_cameras()
        assert_point_display_edit_visibility(multi_point_object, True)
        assert_true(
            settings.camera_edit_mode_active,
            "관측 Camera 선택이 Edit Camera Mode로 전환하지 않았습니다.",
        )
        toggle_camera_mode()
        toggle_point_mode()
        assert_new_point_session_is_clear(multi_point_object, settings)
        toggle_point_mode()

        # 동기화 실패는 기존 Snapshot과 Runtime을 보존하고 잠금 상태로 복구합니다.
        toggle_point_mode()
        select_point_vertices(1)
        toggle_point_mode()
        runtime_before_sync_failure = ensure_runtime(scene)
        snapshot_before_sync_failure = valid_snapshot(
            bpy.context,
            runtime_before_sync_failure,
        )
        state_token_before_sync_failure = (
            settings.edit_revision,
            settings.history_hash,
            runtime_before_sync_failure.index_revision,
            runtime_before_sync_failure.history_hash,
            runtime_before_sync_failure.operations,
        )
        original_clear_selection = (
            point_edit_session._clear_point_edit_selection
        )
        point_edit_session._clear_point_edit_selection = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("의도한 선택 동기화 실패")
            )
        )
        try:
            try:
                sync_failure = bpy.ops.colmap.toggle_edit_point_mode()
            except RuntimeError:
                sync_failure = {"CANCELLED"}
        finally:
            point_edit_session._clear_point_edit_selection = (
                original_clear_selection
            )
        assert_true(
            sync_failure == {"CANCELLED"},
            "선택 동기화 실패가 Edit Point Mode 진입을 취소하지 않았습니다.",
        )
        assert_true(
            multi_point_object.mode == "OBJECT"
            and multi_point_object.hide_select
            and not multi_point_object.select_get(),
            "선택 동기화 실패 뒤 Point Object가 잠금 상태로 복구되지 않았습니다.",
        )
        assert_point_display_edit_visibility(multi_point_object, True)
        assert_true(
            not settings.point_edit_mode_active
            and scene.as_pointer() not in POINT_EDIT_SESSIONS,
            "선택 동기화 실패가 불완전한 Point Session을 남겼습니다.",
        )
        assert_true(
            ensure_runtime(scene) is runtime_before_sync_failure,
            "선택 동기화 실패가 기존 Runtime을 교체했습니다.",
        )
        assert_true(
            valid_snapshot(bpy.context, runtime_before_sync_failure)
            == snapshot_before_sync_failure,
            "선택 동기화 실패가 기존 Snapshot을 손상했습니다.",
        )
        assert_true(
            (
                settings.edit_revision,
                settings.history_hash,
                runtime_before_sync_failure.index_revision,
                runtime_before_sync_failure.history_hash,
                runtime_before_sync_failure.operations,
            )
            == state_token_before_sync_failure,
            "선택 동기화 실패가 revision 또는 편집 이력을 변경했습니다.",
        )
        assert_true(
            point_display_signature(multi_point_object)
            == multi_point_display_before,
            "선택 상태 동기화가 Point 표시 데이터나 Object identity를 변경했습니다.",
        )

        legacy_root = root / "legacy_dataset"
        make_legacy_text(legacy_root)
        settings.dataset_root = str(legacy_root)
        settings.input_format = "TEXT"
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        review_session = ensure_review_session(bpy.context)
        assert_true(
            bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
            settings.review_status,
        )
        assert_true(
            review_session.current_image_id == 7
            and review_session.timeline_active,
            "일반 편집 회귀용 Camera Review Timeline을 준비하지 못했습니다.",
        )

        point_object = managed_objects("POINT_MESH")[0]
        camera_object = managed_objects("CAMERA")[0]
        point_identity = point_object
        camera_identity = camera_object
        assert_true(point_object.hide_select, "Import 직후 Point Object가 잠기지 않았습니다.")
        assert_true(not point_object.select_get(), "Import 직후 Point Object가 선택되었습니다.")
        assert_true(
            bpy.context.view_layer.objects.active is not point_object,
            "Import 직후 Point Object가 Active Object입니다.",
        )
        assert_true(
            camera_object.hide_select and not camera_object.select_get(),
            "Import 직후 Camera Object가 잠기지 않았습니다.",
        )

        # 전용 토글에서만 Point Object를 풀고 Vertex Edit Mode로 진입합니다.
        toggle_point_mode()
        assert_true(not point_object.hide_select, "Edit Point Mode가 선택 잠금을 풀지 않았습니다.")
        assert_true(point_object.mode == "EDIT", "Edit Point Mode가 Blender Edit Mode가 아닙니다.")
        assert_true(
            tuple(bpy.context.tool_settings.mesh_select_mode) == (True, False, False),
            "Edit Point Mode가 Vertex Select를 사용하지 않습니다.",
        )
        point_count_before_guard = len(point_object.data.vertices)
        assert_true(
            bpy.ops.colmap.block_native_delete() == {"FINISHED"},
            "Edit Point Mode의 기본 삭제 차단 경고가 실행되지 않았습니다.",
        )
        assert_true(
            point_object.mode == "EDIT"
            and len(point_object.data.vertices) == point_count_before_guard,
            "기본 삭제 차단 경고가 Point Mesh를 변경하거나 모드를 종료했습니다.",
        )
        select_all_point_vertices()
        toggle_point_mode()
        assert_true(point_object.hide_select, "명시적 모드 종료 뒤 Point가 다시 잠기지 않았습니다.")
        assert_true(settings.selected_point_count == 1, "종료 Snapshot의 선택 수가 잘못되었습니다.")

        # 새 Point 선택 세션은 이전 Blender 정점 선택 상태를 이어받지 않습니다.
        toggle_point_mode()
        bm = bmesh.from_edit_mesh(point_object.data)
        assert_true(
            not any(vertex.select for vertex in bm.verts),
            "Edit Point Mode 재진입 뒤 이전 Point 선택이 남았습니다.",
        )
        select_all_point_vertices()
        toggle_point_mode()

        # 관측 Camera 선택은 Edit Camera Mode로 전환하고 Camera 잠금만 해제합니다.
        select_observing_cameras()
        assert_true(camera_object.select_get(), "Snapshot의 관측 Camera가 선택되지 않았습니다.")
        assert_true(
            settings.camera_edit_mode_active and not camera_object.hide_select,
            "관측 Camera 선택이 Edit Camera Mode를 활성화하지 않았습니다.",
        )
        assert_true(settings.remove_target == "IMAGES", "Camera Remove target이 표시되지 않았습니다.")
        assert_true(
            bpy.ops.colmap.block_native_delete() == {"FINISHED"},
            "Edit Camera Mode의 기본 삭제 차단 경고가 실행되지 않았습니다.",
        )
        assert_true(
            camera_object in managed_objects("CAMERA"),
            "기본 삭제 차단 경고가 관리 Camera를 제거했습니다.",
        )
        toggle_camera_mode()
        assert_true(
            camera_object.hide_select
            and not camera_object.select_get()
            and not settings.camera_edit_mode_active,
            "Edit Camera Mode 종료 뒤 Camera가 다시 잠기지 않았습니다.",
        )
        camera_object.hide_select = False
        camera_object.select_set(True)
        bpy.context.view_layer.objects.active = camera_object
        try:
            remove_outside_mode = bpy.ops.colmap.remove()
        except RuntimeError:
            remove_outside_mode = {"CANCELLED"}
        assert_true(
            remove_outside_mode == {"CANCELLED"} and settings.edit_revision == 0,
            "Edit Camera Mode 밖에서 Camera Remove가 허용되었습니다.",
        )
        camera_object.select_set(False)
        camera_object.hide_select = True
        bpy.context.view_layer.objects.active = None
        toggle_camera_mode()
        assert_true(
            settings.camera_edit_mode_active and not camera_object.hide_select,
            "Edit Camera Mode 버튼이 Camera 잠금을 해제하지 않았습니다.",
        )
        camera_object.select_set(True)
        toggle_camera_mode()
        assert_true(
            camera_object.hide_select and not camera_object.select_get(),
            "Edit Camera Mode 버튼 종료가 Camera를 다시 잠그지 않았습니다.",
        )

        # 외부 Tab/Mode 메뉴 종료는 종료 시 한 번 검증하고 잠금 상태로 정규화합니다.
        toggle_point_mode()
        select_all_point_vertices()
        bpy.ops.object.mode_set(mode="OBJECT")
        normalize_external_mode_change(scene, None)
        assert_true(point_object.hide_select, "외부 Edit Mode 종료 뒤 Point가 잠기지 않았습니다.")
        assert_true(
            not settings.point_edit_mode_active,
            "외부 Edit Mode 종료 뒤 세션 flag가 남았습니다.",
        )
        assert_true(
            settings.selected_point_count == 1,
            "외부 Edit Mode 종료가 유효한 Point 선택을 보존하지 않았습니다.",
        )

        # 선택 누락 같은 일반 사전검사 실패는 Point 선택 모드를 강제 종료하지 않습니다.
        toggle_point_mode()
        bm = bmesh.from_edit_mesh(point_object.data)
        for vertex in bm.verts:
            vertex.select = False
        bmesh.update_edit_mesh(point_object.data)
        try:
            empty_selection = bpy.ops.colmap.select_observing_cameras()
        except RuntimeError:
            empty_selection = {"CANCELLED"}
        assert_true(empty_selection == {"CANCELLED"}, "빈 Point 선택이 허용되었습니다.")
        assert_true(
            point_object.mode == "EDIT" and settings.point_edit_mode_active,
            "빈 선택 사전검사가 Edit Point Mode를 강제 종료했습니다.",
        )
        toggle_point_mode()

        # 외부 종료 시 발견한 비인가 Mesh 변경은 handler 밖의 지연 작업에서 복원합니다.
        toggle_point_mode()
        bm = bmesh.from_edit_mesh(point_object.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 10.0
        bmesh.update_edit_mesh(point_object.data)
        bpy.ops.object.mode_set(mode="OBJECT")
        normalize_external_mode_change(scene, None)
        _restore_pending_point_meshes()
        point_object = managed_objects("POINT_MESH")[0]
        assert_true(point_object.hide_select, "외부 손상 감지 뒤 Point가 잠기지 않았습니다.")
        assert_true(
            abs(point_object.data.vertices[0].co.x - 1.0) < 1.0e-5,
            "외부 종료에서 변경된 Point 좌표가 권위 모델로 복원되지 않았습니다.",
        )

        # 좌표 변경은 COLMAP 편집으로 추측하지 않고 권위 모델에서 복원합니다.
        toggle_point_mode()
        bm = bmesh.from_edit_mesh(point_object.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 10.0
        bm.verts[0].select = True
        bmesh.update_edit_mesh(point_object.data)
        try:
            corrupt_coordinate = bpy.ops.colmap.select_observing_cameras()
        except RuntimeError:
            corrupt_coordinate = {"CANCELLED"}
        assert_true(
            corrupt_coordinate == {"CANCELLED"},
            "변경된 Point 좌표로 관측 Camera 선택이 성공했습니다.",
        )
        point_object = managed_objects("POINT_MESH")[0]
        assert_true(point_object.hide_select, "좌표 손상 복원 뒤 Point가 잠기지 않았습니다.")
        assert_true(
            abs(point_object.data.vertices[0].co.x - 1.0) < 1.0e-5,
            "변경된 Point 좌표가 권위 모델로 복원되지 않았습니다.",
        )

        # topology 변경도 같은 경계에서 거부하고 원래 정점 수로 복원합니다.
        toggle_point_mode()
        bm = bmesh.from_edit_mesh(point_object.data)
        bm.verts.new((9.0, 9.0, 9.0))
        bmesh.update_edit_mesh(point_object.data)
        try:
            corrupt_topology = bpy.ops.colmap.select_observing_cameras()
        except RuntimeError:
            corrupt_topology = {"CANCELLED"}
        assert_true(
            corrupt_topology == {"CANCELLED"},
            "변경된 Point topology로 관측 Camera 선택이 성공했습니다.",
        )
        point_object = managed_objects("POINT_MESH")[0]
        assert_true(len(point_object.data.vertices) == 1, "Point topology가 복원되지 않았습니다.")

        # Scene commit 실패는 Runtime과 history를 변경하지 않고 Point를 잠급니다.
        toggle_point_mode()
        select_all_point_vertices()
        runtime_before_failure = ensure_runtime(scene)
        original_sync = edit_workflows.synchronize_scene
        edit_workflows.synchronize_scene = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("의도한 Scene commit 실패")
        )
        try:
            failed_remove = bpy.ops.colmap.remove()
        except RuntimeError:
            failed_remove = {"CANCELLED"}
        finally:
            edit_workflows.synchronize_scene = original_sync
        assert_true(failed_remove == {"CANCELLED"}, "주입한 Scene 실패가 취소되지 않았습니다.")
        assert_true(settings.edit_revision == 0, "Scene 실패가 history를 변경했습니다.")
        assert_true(
            ensure_runtime(scene) is runtime_before_failure,
            "Scene 실패가 기존 Runtime을 교체했습니다.",
        )
        assert_true(managed_objects("POINT_MESH")[0].hide_select, "실패 뒤 Point가 잠기지 않았습니다.")

        # Point branch는 선택 Point만 제거하고 Image와 Camera를 유지합니다.
        point_object = managed_objects("POINT_MESH")[0]
        settings.camera_frustum_size = 0.73
        toggle_point_mode()
        select_all_point_vertices()
        timeline_marker_token = tuple(
            sorted(
                (
                    marker.name,
                    marker.frame,
                    marker.as_pointer(),
                )
                for marker in scene.timeline_markers
            )
        )
        point_refresh_calls = []
        original_point_refresh = (
            camera_review_timeline.build_or_refresh_timeline
        )

        def count_point_refresh(*args, **kwargs):
            point_refresh_calls.append((args, kwargs))
            return original_point_refresh(*args, **kwargs)

        camera_review_timeline.build_or_refresh_timeline = count_point_refresh
        try:
            point_remove_result = bpy.ops.colmap.remove()
        finally:
            camera_review_timeline.build_or_refresh_timeline = (
                original_point_refresh
            )
        assert_true(
            point_remove_result == {"FINISHED"}
            and not point_refresh_calls,
            "Point 전용 Remove가 불필요하게 Timeline을 재생성했습니다.",
        )
        assert_true(settings.edit_revision == 1, "Point Remove가 revision 하나를 만들지 않았습니다.")
        assert_true(settings.point3d_count == "0", "Point Remove 뒤 Point 수가 잘못되었습니다.")
        assert_true(settings.image_count == "1", "Point Remove가 Image를 제거했습니다.")
        assert_true(len(managed_objects("CAMERA")) == 1, "Point Remove가 Camera를 제거했습니다.")
        assert_true(
            managed_objects("CAMERA")[0] is camera_identity,
            "Point Remove가 생존 Camera Object identity를 바꿨습니다.",
        )
        assert_true(
            abs(camera_identity.data.display_size - 0.73) < 1.0e-6
            and abs(settings.camera_frustum_size - 0.73) < 1.0e-6,
            "Point Remove가 Camera Frustum Size 설정을 기본값으로 되돌렸습니다.",
        )
        assert_true(
            managed_objects("POINT_MESH")[0] is point_identity,
            "Point Remove가 Point Object identity를 바꿨습니다.",
        )
        assert_true(point_identity.hide_select, "Point Remove 성공 뒤 Point가 잠기지 않았습니다.")
        assert_true(camera_identity.hide_select, "Point Remove 뒤 Camera 잠금이 유지되지 않았습니다.")
        review_session = ensure_review_session(bpy.context)
        assert_true(
            review_session.edit_revision == 1
            and review_session.current_image_id == 7
            and review_session.timeline_active
            and timeline_marker_token
            == tuple(
                sorted(
                    (
                        marker.name,
                        marker.frame,
                        marker.as_pointer(),
                    )
                    for marker in scene.timeline_markers
                )
            ),
            "Point Remove 뒤 Review revision 또는 기존 Timeline Marker가 유지되지 않았습니다.",
        )
        runtime = ensure_runtime(scene)
        assert_true(not runtime.current_reconstruction.points3d, "Runtime Point가 제거되지 않았습니다.")
        assert_true(
            7 in runtime.current_reconstruction.images,
            "Point Remove가 Runtime Image를 제거했습니다.",
        )
        assert_true(
            runtime.current_reconstruction.images[7].points2d[0].point3d_id is None,
            "Point Remove가 Point2D 역참조를 정리하지 않았습니다.",
        )

        # Undo는 Point를 복원하고 잠금 상태를 유지합니다.
        assert_true(
            bpy.ops.colmap.undo_last_edit() == {"FINISHED"},
            settings.point_selection_status,
        )
        assert_true(settings.edit_revision == 0, "Undo Last Edit가 revision을 복원하지 않았습니다.")
        assert_true(len(managed_objects("POINT_MESH")[0].data.vertices) == 1, "Point가 복원되지 않았습니다.")
        assert_true(managed_objects("POINT_MESH")[0].hide_select, "Undo 뒤 Point가 잠기지 않았습니다.")
        assert_true(managed_objects("CAMERA")[0].hide_select, "Undo 뒤 Camera가 잠기지 않았습니다.")
        review_session = ensure_review_session(bpy.context)
        assert_true(
            review_session.edit_revision == 0
            and review_session.current_image_id == 7
            and review_session.timeline_active,
            "Undo Last Edit 뒤 Review/Timeline revision이 즉시 갱신되지 않았습니다.",
        )

        # 관측 Camera 선택은 Point mode를 종료하고 Camera target으로 전환합니다.
        point_object = managed_objects("POINT_MESH")[0]
        camera_object = managed_objects("CAMERA")[0]
        camera_object.name = "사용자가_변경한_Camera_이름"
        toggle_point_mode()
        select_all_point_vertices()
        select_observing_cameras()
        assert_true(point_object.hide_select, "Camera 선택 뒤 Point가 잠기지 않았습니다.")
        assert_true(camera_object.select_get(), "이름을 바꾼 관리 Camera가 선택되지 않았습니다.")
        assert_true(
            settings.camera_edit_mode_active and not camera_object.hide_select,
            "Camera 제거 준비가 Edit Camera Mode가 아닙니다.",
        )

        # 비관리 Object 혼합 선택은 Camera 제거 대상을 모호하게 만들므로 거부합니다.
        user_object = bpy.data.objects.new("사용자_Object", None)
        scene.collection.objects.link(user_object)
        user_object.select_set(True)
        try:
            mixed_remove = bpy.ops.colmap.remove()
        except RuntimeError:
            mixed_remove = {"CANCELLED"}
        assert_true(mixed_remove == {"CANCELLED"}, "비관리 Object 혼합 선택이 허용되었습니다.")
        assert_true(settings.edit_revision == 0, "거부된 혼합 선택이 history를 변경했습니다.")
        assert_true(
            settings.camera_edit_mode_active,
            "Camera 선택 사전검사 실패가 Edit Camera Mode를 강제 종료했습니다.",
        )
        user_object.select_set(False)
        camera_object.select_set(True)
        bpy.context.view_layer.objects.active = camera_object

        # 확인 뒤 Camera 선택이 달라지면 저장한 plan을 적용하지 않습니다.
        probe = type("PendingProbe", (), {"_pending": None})()
        _, pending = COLMAP_OT_remove._prepare(probe, bpy.context)
        assert_true(pending.target_kind == "IMAGES", "Pending target이 Camera가 아닙니다.")
        camera_object.select_set(False)
        try:
            COLMAP_OT_remove._prepared_for_execute(probe, bpy.context)
        except ValueError as exc:
            assert_true("변경" in str(exc) or "선택" in str(exc), "stale 오류가 불명확합니다.")
        else:
            raise AssertionError("변경된 Camera 선택으로 pending plan을 적용했습니다.")
        camera_object.select_set(True)
        bpy.context.view_layer.objects.active = camera_object

        # Camera branch는 Image와 그 Image의 모든 관측 Point를 제거합니다.
        assert_true(bpy.ops.colmap.remove() == {"FINISHED"}, settings.point_selection_status)
        assert_true(settings.edit_revision == 1, "Camera Remove가 revision 하나를 만들지 않았습니다.")
        assert_true(settings.image_count == "0", "Camera Remove 뒤 Image가 남았습니다.")
        assert_true(settings.point3d_count == "0", "Camera 관측 Point가 남았습니다.")
        assert_true(not managed_objects("CAMERA"), "제거 Camera Object가 Scene에 남았습니다.")
        assert_true(point_identity.hide_select, "Camera Remove 뒤 Point가 잠기지 않았습니다.")
        review_session = ensure_review_session(bpy.context)
        assert_true(
            (review_session.current_index, review_session.current_image_id)
            == (-1, None)
            and not review_session.timeline_active
            and review_session.timeline_refresh_enabled,
            "일반 Camera Remove 뒤 빈 Review/Timeline 상태가 즉시 갱신되지 않았습니다.",
        )

        # Reset과 같은 source Reload 모두 잠금과 단일 Object identity를 유지합니다.
        assert_true(bpy.ops.colmap.reset_edits() == {"FINISHED"}, settings.point_selection_status)
        assert_true(settings.edit_revision == 0, "Reset Edits가 history를 비우지 않았습니다.")
        assert_true(managed_objects("POINT_MESH")[0].hide_select, "Reset 뒤 Point가 잠기지 않았습니다.")
        assert_true(managed_objects("CAMERA")[0].hide_select, "Reset 뒤 Camera가 잠기지 않았습니다.")
        assert_true(
            abs(managed_objects("CAMERA")[0].data.display_size - 0.73) < 1.0e-6
            and abs(settings.camera_frustum_size - 0.73) < 1.0e-6,
            "Reset Edits가 Camera Frustum Size 설정을 기본값으로 되돌렸습니다.",
        )
        review_session = ensure_review_session(bpy.context)
        assert_true(
            review_session.edit_revision == 0
            and review_session.current_image_id == 7
            and review_session.timeline_active,
            "Reset Edits 뒤 Review/Timeline이 즉시 복구되지 않았습니다.",
        )
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        assert_true(len(managed_objects("POINT_MESH")) == 1, "Reload가 Point 복사본을 만들었습니다.")
        assert_true(
            managed_objects("POINT_MESH")[0] is point_identity,
            "Reload가 Point identity를 바꿨습니다.",
        )
        assert_true(point_identity.hide_select, "Reload 뒤 Point가 잠기지 않았습니다.")
        assert_true(managed_objects("CAMERA")[0].hide_select, "Reload 뒤 Camera가 잠기지 않았습니다.")

        # 저장·재열기 뒤 Runtime replay와 잠금 상태를 확인합니다.
        toggle_point_mode()
        select_all_point_vertices()
        assert_true(bpy.ops.colmap.remove() == {"FINISHED"}, settings.point_selection_status)
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            "schema 2 history가 있는 같은 source Reload에 실패했습니다.",
        )
        assert_true(settings.edit_revision == 1, "Reload가 schema 2 history를 잃었습니다.")
        assert_true(
            not ensure_runtime(scene).current_reconstruction.points3d,
            "Reload replay 결과가 다릅니다.",
        )
        point_identity.hide_select = False
        point_identity.select_set(True)
        bpy.context.view_layer.objects.active = point_identity
        blend_path = root / "feature2_redesign.blend"
        assert_true(
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_path)) == {"FINISHED"},
            ".blend 저장에 실패했습니다.",
        )
        clear_runtime()
        assert_true(
            bpy.ops.wm.open_mainfile(filepath=str(blend_path)) == {"FINISHED"},
            ".blend 재열기에 실패했습니다.",
        )
        scene = bpy.context.scene
        settings = scene.colmap_import
        reopened = ensure_runtime(scene)
        assert_true(settings.edit_revision == 1, "재열기 뒤 schema 2 이력이 유지되지 않았습니다.")
        assert_true(not reopened.current_reconstruction.points3d, "재열기 replay가 Point를 복원했습니다.")
        assert_true(managed_objects("POINT_MESH")[0].hide_select, "재열기 뒤 Point가 잠기지 않았습니다.")
        assert_true(managed_objects("CAMERA")[0].hide_select, "재열기 뒤 Camera가 잠기지 않았습니다.")

        # 실제 다중 센서 Rig의 Camera branch는 Core 사전검사에서 거부합니다.
        modern_root = root / "modern_dataset"
        make_modern_text(modern_root)
        settings.dataset_root = str(modern_root)
        settings.input_format = "TEXT"
        assert_true(bpy.ops.colmap.import_model() == {"FINISHED"}, settings.status)
        toggle_point_mode()
        select_all_point_vertices()
        select_observing_cameras()
        try:
            rig_remove = bpy.ops.colmap.remove()
        except RuntimeError:
            rig_remove = {"CANCELLED"}
        assert_true(
            rig_remove == {"CANCELLED"},
            "실제 다중 센서 Rig의 부분 Image 삭제가 허용되었습니다.",
        )
        assert_true(settings.edit_revision == 0, "거부된 Rig 삭제가 history를 변경했습니다.")
        assert_true(managed_objects("POINT_MESH")[0].hide_select, "Rig 거부 뒤 Point가 잠기지 않았습니다.")
        assert_true(
            settings.camera_edit_mode_active,
            "Rig 사전검사 거부가 Edit Camera Mode를 강제 종료했습니다.",
        )
finally:
    blender_colmap.unregister()

assert_true(
    invalidate_loaded_state not in bpy.app.handlers.load_post,
    "Extension 해제 뒤 Load Runtime 무효화 handler가 남았습니다.",
)
for handlers in (
    bpy.app.handlers.undo_post,
    bpy.app.handlers.redo_post,
):
    assert_true(
        reconcile_after_undo_redo not in handlers,
        "Extension 해제 뒤 Undo/Redo 재연결 handler가 남았습니다.",
    )
assert_true(
    normalize_external_mode_change not in bpy.app.handlers.depsgraph_update_post,
    "Extension 해제 뒤 외부 Point Mode 종료 handler가 남았습니다.",
)
assert_true(
    not bpy.app.timers.is_registered(_restore_pending_point_meshes),
    "Extension 해제 뒤 Point Mesh 지연 복원 timer가 남았습니다.",
)
assert_true(not _KEYMAP_ITEMS, "Extension 해제 뒤 X/Delete 차단 키맵이 남았습니다.")
print("BLENDER_COLMAP_BACKGROUND_POINT_SELECTION_OK")
