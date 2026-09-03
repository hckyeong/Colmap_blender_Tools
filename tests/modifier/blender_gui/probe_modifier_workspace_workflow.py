"""전용 Workspace에서 실제 Import·Edit·Review·Export 흐름을 검증합니다."""

from __future__ import annotations

import bmesh
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

USE_INSTALLED = os.environ.get("BLENDER_COLMAP_USE_INSTALLED") == "1"
PACKAGE_NAME = (
    "bl_ext.user_default.colmap_tools.tools.modifier"
    if USE_INSTALLED
    else "tools.modifier"
)
blender_colmap = importlib.import_module(PACKAGE_NAME)
ownership = importlib.import_module(f"{PACKAGE_NAME}.blender.ownership")
runtime_state = importlib.import_module(f"{PACKAGE_NAME}.blender.runtime_state")
modifier_runtime_domain = importlib.import_module(
    f"{PACKAGE_NAME}.blender.modifier_runtime_domain"
)
modifier_session = importlib.import_module(
    f"{PACKAGE_NAME}.blender.modifier_session"
)
modifier_viewport = importlib.import_module(
    f"{PACKAGE_NAME}.blender.modifier_viewport"
)
modifier_data_visibility = importlib.import_module(
    f"{PACKAGE_NAME}.blender.modifier_data_visibility"
)
camera_review_keymap = importlib.import_module(
    f"{PACKAGE_NAME}.blender.camera_review_keymap"
)
camera_review_state = importlib.import_module(
    f"{PACKAGE_NAME}.blender.camera_review_session"
)
managed_scene = importlib.import_module(f"{PACKAGE_NAME}.blender.managed_scene")
binary_io = importlib.import_module(f"{PACKAGE_NAME}.core.binary_io")

SESSION = modifier_session.SESSION
SESSION_ACTIVE = modifier_session.SESSION_ACTIVE


_STAGE = "OPEN"
_STARTED_AT = time.monotonic()
_TEMP_DIRECTORY = tempfile.TemporaryDirectory()
_MODEL_DIRECTORY: Path | None = None
_DEFAULT_OBJECT_HIDE_STATES = {}
_MANAGED_OBJECT_POINTERS: tuple[int, ...] = ()
_SOURCE_WORKSPACE_POINTER = 0


def _assert(condition, message: str) -> None:
    """조건이 거짓이면 실제 Workflow 실패 원인을 보고합니다."""

    if not condition:
        raise AssertionError(message)


def _make_dataset(root: Path) -> Path:
    """GUI Workflow에 사용할 단일 Camera·Point COLMAP TXT 모델을 만듭니다."""

    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame.ppm").write_bytes(
        b"P6\n2 2\n255\n" + bytes((255, 64, 32) * 4)
    )
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 64 64 50 50 32 32\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n"
        "7 1 0 0 0 0 0 0 1 frame.ppm\n"
        "32 32 100\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n100 0 0 2 255 96 64 0.1 7 0\n",
        encoding="utf-8",
    )
    return model


def _control_override(window) -> dict[str, object]:
    """전용 3D View의 WINDOW Region Context를 반환합니다."""

    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "WINDOW")
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
    }


def _managed_point_object(settings):
    """현재 Dataset이 소유한 Point Mesh Object를 반환합니다."""

    return next(
        obj
        for obj in bpy.data.objects
        if obj.get(ownership.OWNER_KEY) == ownership.OWNER_TOKEN
        and obj.get(ownership.RECORD_KIND_KEY) == "POINT_MESH"
        and obj.get(ownership.DATASET_UUID_KEY) == settings.dataset_uuid
        and obj.users_collection
    )


def _managed_scene_objects(settings) -> tuple[object, ...]:
    """현재 Dataset의 Point와 Camera 관리 Object를 데이터블록에서 반환합니다."""

    return tuple(
        obj
        for obj in bpy.data.objects
        if obj.get(ownership.OWNER_KEY) == ownership.OWNER_TOKEN
        and obj.get(ownership.DATASET_UUID_KEY) == settings.dataset_uuid
        and obj.get(ownership.RECORD_KIND_KEY) in {"POINT_MESH", "CAMERA"}
        and obj.users_collection
    )


def _objects_by_pointer(pointers: tuple[int, ...]) -> tuple[object, ...]:
    """저장한 Pointer와 일치하는 현재 Object 데이터블록을 반환합니다."""

    pointer_set = set(pointers)
    return tuple(obj for obj in bpy.data.objects if obj.as_pointer() in pointer_set)


def _simulate_f7(window) -> None:
    """Modifier Control View 중앙에 실제 F7 입력과 해제 이벤트를 보냅니다."""

    override = _control_override(window)
    region = override["region"]
    x = int(region.x + max(region.width // 2, 1))
    y = int(region.y + max(region.height // 2, 1))
    window.event_simulate(type="F7", value="PRESS", x=x, y=y)
    window.event_simulate(type="F7", value="RELEASE", x=x, y=y)


def _simulate_modifier_undo(window, *, redo: bool = False) -> None:
    """Modifier Control View에 실제 Ctrl+Z 또는 Ctrl+Shift+Z 입력을 보냅니다."""

    override = _control_override(window)
    region = override["region"]
    x = int(region.x + max(region.width // 2, 1))
    y = int(region.y + max(region.height // 2, 1))
    window.event_simulate(
        type="Z",
        value="PRESS",
        x=x,
        y=y,
        ctrl=True,
        shift=redo,
    )
    window.event_simulate(
        type="Z",
        value="RELEASE",
        x=x,
        y=y,
        ctrl=True,
        shift=redo,
    )


def _remove_point(settings) -> None:
    """실제 Point Edit Mode에서 첫 Point를 선택해 Remove를 실행합니다."""

    _assert(
        bpy.ops.colmap.toggle_edit_point_mode() == {"FINISHED"},
        settings.point_selection_status,
    )
    point_object = _managed_point_object(settings)
    mesh = bmesh.from_edit_mesh(point_object.data)
    mesh.verts.ensure_lookup_table()
    for vertex in mesh.verts:
        vertex.select = False
    mesh.verts[0].select = True
    mesh.select_history.clear()
    mesh.select_history.add(mesh.verts[0])
    bmesh.update_edit_mesh(point_object.data)
    _assert(
        bpy.ops.colmap.remove() == {"FINISHED"},
        settings.point_selection_status,
    )


def _workflow_tick():
    """50초 제한 안에서 실제 Modifier Workflow를 단계별 검증합니다."""

    global _MANAGED_OBJECT_POINTERS
    global _MODEL_DIRECTORY
    global _STAGE
    window = bpy.context.window

    try:
        if time.monotonic() - _STARTED_AT > 50.0:
            settings = getattr(bpy.context.scene, "colmap_import", None)
            raise AssertionError(
                "Workflow가 50초 안에 끝나지 않았습니다: "
                f"{_STAGE}, phase={SESSION.phase}, "
                f"session={SESSION.status_message!r}, "
                f"status={getattr(settings, 'status', '')!r}"
            )
        if _STAGE == "OPEN":
            _assert(
                bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT") == {"FINISHED"},
                "COLMAP Modifier Workspace 진입에 실패했습니다.",
            )
            _STAGE = "WAIT_ACTIVE"
            return 0.05

        if _STAGE == "WAIT_ACTIVE":
            if SESSION.phase != SESSION_ACTIVE:
                return 0.05
            _assert(
                modifier_data_visibility.is_modifier_data_view_layer(window.view_layer),
                "Modifier Workspace가 데이터 전용 View Layer를 사용하지 않습니다.",
            )
            _assert(
                all(
                    (obj := window.view_layer.objects.get(object_name)) is None
                    or not obj.visible_get(view_layer=window.view_layer)
                    for object_name in _DEFAULT_OBJECT_HIDE_STATES
                ),
                "Modifier 전용 View Layer에서 일반 Scene Object가 계속 보입니다.",
            )
            root = Path(_TEMP_DIRECTORY.name)
            _MODEL_DIRECTORY = _make_dataset(root)
            with bpy.context.temp_override(**_control_override(window)):
                region_3d = bpy.context.area.spaces.active.region_3d
                region_3d.view_location = (1000.0, 1000.0, 1000.0)
                region_3d.view_distance = 0.1
                settings = bpy.context.scene.colmap_import
                settings.dataset_root = str(root)
                settings.input_format = "TEXT"
                _assert(
                    bpy.ops.colmap.import_model("EXEC_DEFAULT") == {"FINISHED"},
                    settings.status,
                )
                _assert(
                    (settings.camera_count, settings.image_count, settings.point3d_count)
                    == ("1", "1", "1"),
                    "전용 Workspace Import 결과 Count가 잘못되었습니다.",
                )
                domain = modifier_runtime_domain.domain_for_scene(
                    bpy.context.scene,
                    required=True,
                )
                _assert(
                    domain.dataset_state is runtime_state.ensure_runtime(
                        bpy.context.scene
                    )
                    and domain.journal.revision == 0,
                    "전용 Workspace Import가 독립 Runtime과 빈 Journal을 만들지 못했습니다.",
                )
                managed_objects = _managed_scene_objects(settings)
                _assert(
                    len(managed_objects) == 2
                    and all(
                        obj.visible_get(view_layer=window.view_layer)
                        for obj in managed_objects
                    ),
                    "Modifier 전용 View Layer에 Import Object가 모두 표시되지 않았습니다.",
                )
                _MANAGED_OBJECT_POINTERS = tuple(
                    obj.as_pointer() for obj in managed_objects
                )
                _assert(
                    region_3d.view_perspective == "PERSP"
                    and max(
                        abs(float(value) - 1000.0)
                        for value in region_3d.view_location
                    )
                    > 1.0,
                    "COLMAP Point 범위의 자동 Frame All이 실행되지 않았습니다.",
                )
                _assert(
                    bpy.ops.colmap.review_look_through("EXEC_DEFAULT") == {"FINISHED"},
                    settings.review_status,
                )
                _assert(
                    len(camera_review_keymap._KEYMAP_ITEMS) == 1
                    and camera_review_keymap._KEYMAP_ITEMS[0][1].idname
                    == "colmap.review_background_toggle"
                    and camera_review_keymap._KEYMAP_ITEMS[0][1].type == "F7",
                    "Modifier F7 Camera Review 키맵이 등록되지 않았습니다.",
                )
                _assert(
                    settings.review_background_enabled,
                    "F7 회귀 시작 전 Background Image가 꺼져 있습니다.",
                )
                _simulate_f7(window)
            _STAGE = "WAIT_F7_OFF"
            return 0.1

        if _STAGE == "WAIT_F7_OFF":
            with bpy.context.temp_override(**_control_override(window)):
                settings = bpy.context.scene.colmap_import
                if settings.review_background_enabled:
                    return 0.05
                review_session = camera_review_state.current_review_session(
                    bpy.context,
                    strict=True,
                )
                managed_slots = tuple(review_session.background_slots.values())
                _assert(
                    managed_slots
                    and all(not slot.show_background_image for slot in managed_slots),
                    "F7 OFF가 Modifier 관리 Background Image에 반영되지 않았습니다.",
                )
                _simulate_f7(window)
            _STAGE = "WAIT_F7_ON"
            return 0.1

        if _STAGE == "WAIT_F7_ON":
            with bpy.context.temp_override(**_control_override(window)):
                settings = bpy.context.scene.colmap_import
                if not settings.review_background_enabled:
                    return 0.05
                review_session = camera_review_state.current_review_session(
                    bpy.context,
                    strict=True,
                )
                managed_slots = tuple(review_session.background_slots.values())
                _assert(
                    managed_slots
                    and all(slot.show_background_image for slot in managed_slots),
                    "F7 ON이 Modifier 관리 Background Image에 반영되지 않았습니다.",
                )
                _assert(
                    bpy.ops.colmap.review_filter_current("EXEC_DEFAULT") == {"FINISHED"}
                    and settings.review_pending_camera_count == 1
                    and settings.edit_revision == 0,
                    "전용 Workspace Pending Filter 등록이 실패했습니다.",
                )
                _assert(
                    bpy.ops.colmap.review_undo_last_filter("EXEC_DEFAULT") == {"FINISHED"}
                    and settings.review_pending_camera_count == 0
                    and settings.edit_revision == 0,
                    "전용 Workspace Undo Last Filter 복원이 실패했습니다.",
                )
                _remove_point(settings)
                domain = modifier_runtime_domain.domain_for_scene(
                    bpy.context.scene,
                    required=True,
                )
                _assert(
                    settings.edit_revision == 1
                    and domain.journal.revision == 1
                    and domain.journal.poll_undo(),
                    "Point Remove가 Modifier Runtime과 전용 Journal을 갱신하지 않았습니다.",
                )
                _simulate_modifier_undo(window)
            _STAGE = "WAIT_UNDO"
            return 0.1

        if _STAGE == "WAIT_UNDO":
            with bpy.context.temp_override(**_control_override(window)):
                settings = bpy.context.scene.colmap_import
                state = runtime_state.ensure_runtime(bpy.context.scene)
                if settings.edit_revision != 0 or len(state.current_reconstruction.points3d) != 1:
                    return 0.1
                domain = modifier_runtime_domain.domain_for_scene(
                    bpy.context.scene,
                    required=True,
                )
                _assert(
                    domain.journal.poll_redo() and not domain.journal.poll_undo(),
                    "Ctrl+Z가 Modifier 전용 Journal만 되돌리지 못했습니다.",
                )
                _assert(
                    SESSION.phase == SESSION_ACTIVE,
                    "Modifier Undo 뒤 Workspace Session이 비활성화되었습니다.",
                )
                _simulate_modifier_undo(window, redo=True)
            _STAGE = "WAIT_REDO"
            return 0.1

        if _STAGE == "WAIT_REDO":
            with bpy.context.temp_override(**_control_override(window)):
                settings = bpy.context.scene.colmap_import
                state = runtime_state.ensure_runtime(bpy.context.scene)
                if settings.edit_revision != 1 or state.current_reconstruction.points3d:
                    return 0.1
                domain = modifier_runtime_domain.domain_for_scene(
                    bpy.context.scene,
                    required=True,
                )
                _assert(
                    domain.journal.poll_undo() and not domain.journal.poll_redo(),
                    "Ctrl+Shift+Z가 Modifier 전용 Journal을 다시 적용하지 못했습니다.",
                )
                _simulate_modifier_undo(window)
            _STAGE = "WAIT_SECOND_UNDO"
            return 0.1

        if _STAGE == "WAIT_SECOND_UNDO":
            with bpy.context.temp_override(**_control_override(window)):
                settings = bpy.context.scene.colmap_import
                state = runtime_state.ensure_runtime(bpy.context.scene)
                if settings.edit_revision != 0 or len(state.current_reconstruction.points3d) != 1:
                    return 0.1
                _remove_point(settings)
                settings.export_backup_enabled = False
                settings.export_output_format = "BINARY"
                _assert(
                    bpy.ops.colmap.export("EXEC_DEFAULT") == {"FINISHED"},
                    settings.export_status,
                )
                domain = modifier_runtime_domain.domain_for_scene(
                    bpy.context.scene,
                    required=True,
                )
                _assert(
                    domain.journal.revision == 0
                    and not domain.journal.poll_undo()
                    and not domain.journal.poll_redo(),
                    "Export가 새 기준선과 빈 Modifier Journal을 만들지 않았습니다.",
                )
                exported = binary_io.read_model_binary(_MODEL_DIRECTORY, modern=False)
                _assert(
                    not exported.points3d and "BIN" in settings.export_status,
                    "전용 Workspace BIN Export 결과가 잘못되었습니다.",
                )
                _assert(
                    bpy.ops.colmap.review_filter_current("EXEC_DEFAULT") == {"FINISHED"}
                    and settings.review_pending_camera_count == 1,
                    "Workspace 재진입 검증용 Pending Filter 등록이 실패했습니다.",
                )
                _assert(
                    bpy.ops.colmap.exit_modifier_workspace("EXEC_DEFAULT") == {"FINISHED"},
                    "실제 Workflow 뒤 Workspace 종료에 실패했습니다.",
                )
            _STAGE = "REOPEN"
            return 0.05

        if _STAGE == "REOPEN":
            if window.workspace.as_pointer() != _SOURCE_WORKSPACE_POINTER:
                return 0.05
            managed_objects = _objects_by_pointer(_MANAGED_OBJECT_POINTERS)
            _assert(
                len(managed_objects) == len(_MANAGED_OBJECT_POINTERS),
                "Workspace 종료 중 Modifier 관리 Object 데이터가 삭제되었습니다.",
            )
            _assert(
                not modifier_data_visibility.is_modifier_data_view_layer(window.view_layer)
                and all(
                    not obj.visible_get(view_layer=window.view_layer)
                    for obj in managed_objects
                ),
                "일반 Workspace에서 Modifier Import 데이터가 숨겨지지 않았습니다.",
            )
            _assert(
                bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT") == {"FINISHED"},
                "Pending 상태를 보존한 Workspace 재진입에 실패했습니다.",
            )
            _STAGE = "WAIT_REENTER"
            return 0.05

        if _STAGE == "WAIT_REENTER":
            if SESSION.phase != SESSION_ACTIVE:
                return 0.05
            managed_objects = _objects_by_pointer(_MANAGED_OBJECT_POINTERS)
            managed_visibility = tuple(
                obj.visible_get(view_layer=window.view_layer)
                for obj in managed_objects
            )
            managed_roots = tuple(managed_scene.iter_managed_roots(window.scene))
            roots_included = all(
                not modifier_data_visibility._layer_collection_for_collection(
                    window.view_layer.layer_collection,
                    root,
                ).exclude
                for root in managed_roots
            )
            _assert(
                modifier_data_visibility.is_modifier_data_view_layer(window.view_layer)
                and roots_included
                and any(managed_visibility),
                "Modifier 재진입 뒤 전용 View Layer 데이터가 복원되지 않았습니다: "
                f"workspace={window.workspace.name!r}, "
                f"view_layer={window.view_layer.name!r}, "
                f"managed_visibility={managed_visibility!r}",
            )
            with bpy.context.temp_override(**_control_override(window)):
                settings = bpy.context.scene.colmap_import
                _assert(
                    settings.review_pending_camera_count == 1,
                    "Workspace 재진입 뒤 Pending Filter가 복원되지 않았습니다.",
                )
                _assert(
                    bpy.ops.colmap.review_undo_last_filter("EXEC_DEFAULT") == {"FINISHED"}
                    and settings.review_pending_camera_count == 0,
                    "재진입한 Workspace에서 Pending Undo가 실패했습니다.",
                )
                _assert(
                    bpy.ops.colmap.exit_modifier_workspace("EXEC_DEFAULT") == {"FINISHED"},
                    "재진입 Session 종료에 실패했습니다.",
                )
            _STAGE = "DONE"
            return 0.05

        if _STAGE == "DONE":
            if window.workspace.as_pointer() != _SOURCE_WORKSPACE_POINTER:
                return 0.05
            managed_objects = _objects_by_pointer(_MANAGED_OBJECT_POINTERS)
            _assert(
                not modifier_data_visibility.is_modifier_data_view_layer(window.view_layer)
                and all(
                    not obj.visible_get(view_layer=window.view_layer)
                    for obj in managed_objects
                ),
                "최종 종료 뒤 일반 Workspace에 Modifier 데이터가 다시 표시되었습니다.",
            )
            for object_name, hidden in _DEFAULT_OBJECT_HIDE_STATES.items():
                obj = window.view_layer.objects.get(object_name)
                _assert(
                    obj is None
                    or obj.hide_get(view_layer=window.view_layer) == hidden,
                    f"Global Undo 뒤 {object_name} 숨김 상태가 복원되지 않았습니다.",
                )
            print("COLMAP_MODIFIER_WORKSPACE_WORKFLOW_GUI_OK")
            _TEMP_DIRECTORY.cleanup()
            blender_colmap.unregister()
            bpy.ops.wm.quit_blender()
            return None
    except Exception as error:
        print(f"COLMAP_MODIFIER_WORKSPACE_WORKFLOW_GUI_FAIL: {error}")
        _TEMP_DIRECTORY.cleanup()
        try:
            blender_colmap.unregister()
        finally:
            bpy.ops.wm.quit_blender()
        raise
    return 0.05


def main() -> None:
    """Extension을 등록하고 실제 Workflow Timer를 시작합니다."""

    global _DEFAULT_OBJECT_HIDE_STATES
    global _SOURCE_WORKSPACE_POINTER
    _SOURCE_WORKSPACE_POINTER = bpy.context.window.workspace.as_pointer()
    blender_colmap.register()
    _DEFAULT_OBJECT_HIDE_STATES = {
        object_name: bool(obj.hide_get(view_layer=bpy.context.view_layer))
        for object_name, object_type in modifier_viewport.DEFAULT_STARTUP_OBJECTS
        if (obj := bpy.context.view_layer.objects.get(object_name)) is not None
        and obj.type == object_type
    }
    bpy.app.timers.register(_workflow_tick, first_interval=0.1)


main()
