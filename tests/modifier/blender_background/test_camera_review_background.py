"""Blender 5.2 background에서 Camera Review 단계 2~5 계약을 검증합니다."""

from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier as blender_colmap
from tools.modifier.blender import (
    camera_filter_queue,
    camera_review_keymap,
    camera_review_images,
    camera_review_session,
    camera_review_timeline,
    camera_review_viewport,
    edit_workflows,
    lifecycle,
    modifier_session,
    modifier_workspace,
    operators_import,
    state_handlers,
)
from tools.modifier.blender.runtime_state import clear_runtime, ensure_runtime
from tools.modifier.blender.camera_review_images import (
    REVIEW_IMAGE_KIND,
    managed_review_images,
)
from tools.modifier.blender.camera_review_session import (
    current_review_session,
    ensure_review_session,
    navigate_review,
)
from tools.modifier.blender.camera_review_viewport import (
    look_through_current,
    restore_previous_view,
)
from tools.modifier.blender.managed_scene import (
    managed_camera_map,
    require_managed_point_object,
)
from tools.modifier.blender.operators_camera_review import (
    COLMAP_OT_review_apply_pending_filters,
    COLMAP_OT_review_background_toggle,
    COLMAP_OT_review_build_timeline,
    COLMAP_OT_review_clear_pending_filters,
    COLMAP_OT_review_filter_current,
    COLMAP_OT_review_look_through,
    COLMAP_OT_review_next,
    COLMAP_OT_review_previous,
    COLMAP_OT_review_undo_last_filter,
)
from tools.modifier.blender.ownership import (
    DATASET_UUID_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    SOURCE_FINGERPRINT_KEY,
)
from tools.modifier.blender.panels import CLASSES as PANEL_CLASSES
from tools.modifier.blender.modifier_workspace_contract import MODIFIER_WORKSPACE_SPEC
from tests.modifier.fixture_utils import make_modern_text
from shared.blender_runtime.workspace_factory import (
    STATE_READY,
    WORKSPACE_OWNER_KEY as SHARED_WORKSPACE_OWNER_KEY,
    WORKSPACE_SCHEMA_KEY,
    WORKSPACE_SEED_SCHEMA_KEY,
    WORKSPACE_STATE_KEY,
    WORKSPACE_TOOL_ID_KEY,
    WORKSPACE_UUID_KEY,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _activate_modifier_camera_session_for_test(scene) -> None:
    """현재 Background Window를 유효한 Modifier Camera 세션으로 활성화합니다."""

    window = bpy.context.window
    workspace = window.workspace
    workspace[SHARED_WORKSPACE_OWNER_KEY] = "colmap_tools"
    workspace[WORKSPACE_TOOL_ID_KEY] = MODIFIER_WORKSPACE_SPEC.tool_id
    workspace[WORKSPACE_UUID_KEY] = "modifier-camera-review-background"
    workspace[WORKSPACE_SCHEMA_KEY] = MODIFIER_WORKSPACE_SPEC.workspace_schema
    workspace[WORKSPACE_SEED_SCHEMA_KEY] = MODIFIER_WORKSPACE_SPEC.seed_schema
    workspace[WORKSPACE_STATE_KEY] = STATE_READY
    session = modifier_session.SESSION
    session.generation += 1
    session.phase = modifier_session.SESSION_ACTIVE
    session.target_window_ref = window
    session.target_window_pointer = window.as_pointer()
    session.modifier_workspace_ref = workspace
    session.modifier_workspace_pointer = workspace.as_pointer()
    session.modifier_workspace_name = workspace.name
    control_area = next(
        (area for area in window.screen.areas if area.type == "VIEW_3D"),
        next(iter(window.screen.areas)),
    )
    session.bind_control_area(window, control_area)
    camera_review_timeline.release_modifier_marker_session(scene)
    camera_review_timeline.acquire_modifier_marker_session(scene, window=window)


def _write_ppm(path: Path, rgb: tuple[int, int, int]) -> None:
    """외부 이미지 라이브러리 없이 Blender가 읽을 수 있는 2x2 PPM을 만듭니다."""
    pixels = bytes(rgb * 4)
    path.write_bytes(b"P6\n2 2\n255\n" + pixels)


def _make_review_fixture(root: Path) -> tuple[Path, Path]:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    for name, color in (
        ("two.ppm", (255, 0, 0)),
        ("five.ppm", (0, 255, 0)),
        ("nine.ppm", (0, 0, 255)),
        ("twelve.ppm", (255, 255, 0)),
    ):
        _write_ppm(images / name, color)
    (model / "cameras.txt").write_text(
        "# cameras\n"
        "1 PINHOLE 640 480 510 500 320 240\n"
        "2 OPENCV 640 480 500 500 320 240 0 0 0 0\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n"
        "9 1 0 0 0 0 0 0 1 nine.ppm\n"
        "30 30 300\n"
        "2 1 0 0 0 1 0 0 1 two.ppm\n"
        "10 10 100 20 20 200\n"
        "12 1 0 0 0 2 0 0 2 twelve.ppm\n\n"
        "5 1 0 0 0 3 0 0 1 five.ppm\n"
        "20 20 200 30 30 300\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n"
        "100 0 0 0 255 0 0 0.1 2 0\n"
        "200 1 0 0 0 255 0 0.1 2 1 5 0\n"
        "300 2 0 0 0 0 255 0.1 5 1 9 0\n",
        encoding="utf-8",
    )
    return model, images


class _Pointer:
    _next_pointer = 100_000

    def __init__(self):
        self._pointer = _Pointer._next_pointer
        _Pointer._next_pointer += 1

    def as_pointer(self):
        return self._pointer


class _Area(_Pointer):
    type = "VIEW_3D"

    def __init__(self):
        super().__init__()
        self.redraw_count = 0

    def tag_redraw(self):
        self.redraw_count += 1


class _Region(_Pointer):
    def __init__(self, region_type="WINDOW"):
        super().__init__()
        self.type = region_type


class _RegionView3D:
    def __init__(self):
        self.view_perspective = "PERSP"
        self.view_location = Vector((1.0, 2.0, 3.0))
        self.view_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
        self.view_distance = 8.0
        self.view_camera_zoom = 1.5
        self.view_camera_offset = Vector((0.25, -0.5))


class _Space:
    def __init__(self):
        self.region_3d = _RegionView3D()
        self.camera = None


class _ViewportContext:
    def __init__(self, scene, region_type="WINDOW"):
        self.scene = scene
        self.area = _Area()
        self.window_region = _Region()
        self.region = (
            self.window_region
            if region_type == "WINDOW"
            else _Region(region_type)
        )
        self.area.regions = (self.window_region, self.region)
        self.space_data = _Space()


class _RecordingLayout:
    """Camera Review 패널이 노출한 Property와 Operator만 기록합니다."""

    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []
        self.enabled = True

    def box(self):
        return _RecordingLayout(self.calls)

    def row(self, **_kwargs):
        return _RecordingLayout(self.calls)

    def column(self, **_kwargs):
        return _RecordingLayout(self.calls)

    def label(self, **kwargs):
        self.calls.append(
            ("LABEL", kwargs.get("text", ""), kwargs.get("icon", "NONE"))
        )

    def prop(self, _data, property_name, **_kwargs):
        self.calls.append(("PROP", property_name))
        self.calls.append(("PROP_OPTIONS", property_name, dict(_kwargs)))

    def operator(self, operator_id, **_kwargs):
        self.calls.append(("OPERATOR", operator_id))
        return None


blender_colmap.register()
try:
    scene = bpy.context.scene
    settings = scene.colmap_import
    camera_review_timeline.acquire_modifier_marker_session(scene)
    assert_true(
        len(camera_review_keymap._KEYMAP_ITEMS) == 1
        and camera_review_keymap._KEYMAP_ITEMS[0][0].name == "3D View"
        and camera_review_keymap._KEYMAP_ITEMS[0][1].idname
        == "colmap.review_background_toggle"
        and camera_review_keymap._KEYMAP_ITEMS[0][1].type == "F7"
        and camera_review_keymap._KEYMAP_ITEMS[0][1].value == "PRESS",
        "Modifier Camera Review F7 키맵이 정확히 하나 등록되지 않았습니다.",
    )
    assert_true(
        not COLMAP_OT_review_background_toggle.poll(bpy.context),
        "비활성 Modifier 세션에서 F7 Background 토글이 허용되었습니다.",
    )
    _activate_modifier_camera_session_for_test(scene)
    camera_review_timeline.register_frame_change_handler()
    camera_review_timeline.register_frame_change_handler()
    assert_true(
        bpy.app.handlers.frame_change_post.count(
            camera_review_timeline.frame_change_post
        )
        == 1,
        "Camera Review frame handler가 정확히 한 번 등록되지 않았습니다.",
    )
    assert_true(
        bpy.app.handlers.depsgraph_update_post.count(
            camera_review_timeline.timeline_marker_guard_post
        )
        == 1,
        "Camera Review Marker 보호 handler가 정확히 한 번 등록되지 않았습니다.",
    )

    expected_properties = {
        "review_current_index": ("INT", -1),
        "review_current_image_id": ("STRING", ""),
        "review_total_count": ("INT", 0),
        "review_camera_model": ("STRING", ""),
        "review_image_status": ("STRING", ""),
        "review_exact_projection": ("BOOLEAN", False),
        "review_status": ("STRING", "Camera Review 준비 중"),
        "review_frame_start": ("INT", 1),
        "review_background_alpha": ("FLOAT", 1.0),
        "review_background_enabled": ("BOOLEAN", True),
        "review_marker_manifest_json": ("STRING", "[]"),
        "review_vram_window": ("STRING", ""),
        "review_restore_available": ("BOOLEAN", False),
        "review_pending_filter_json": ("STRING", "{}"),
        "review_pending_filter_order_json": ("STRING", "[]"),
        "review_pending_camera_count": ("INT", 0),
        "review_pending_point_count": ("INT", 0),
        "review_pending_filter_status": ("STRING", "EMPTY"),
    }
    for name, (property_type, default) in expected_properties.items():
        prop = settings.bl_rna.properties[name]
        assert_true(prop.type == property_type, f"{name} Property 타입이 잘못되었습니다.")
        assert_true(getattr(settings, name) == default, f"{name} 기본값이 잘못되었습니다.")

    assert_true(
        tuple(panel.bl_idname for panel in PANEL_CLASSES)[-2:]
        == ("COLMAP_PT_camera_review", "COLMAP_PT_export"),
        "Camera Review와 Export 패널 등록 순서가 잘못되었습니다.",
    )
    operator_contract = {
        COLMAP_OT_review_previous: ("colmap.review_previous", "Previous"),
        COLMAP_OT_review_next: ("colmap.review_next", "Next"),
        COLMAP_OT_review_look_through: (
            "colmap.review_look_through",
            "Look Through Camera",
        ),
        COLMAP_OT_review_background_toggle: (
            "colmap.review_background_toggle",
            "Toggle Background Image",
        ),
        COLMAP_OT_review_build_timeline: (
            "colmap.review_build_timeline",
            "Build / Refresh Timeline",
        ),
        COLMAP_OT_review_filter_current: (
            "colmap.review_filter_current",
            "Filter Current Camera",
        ),
        COLMAP_OT_review_undo_last_filter: (
            "colmap.review_undo_last_filter",
            "Undo Last Filter",
        ),
        COLMAP_OT_review_apply_pending_filters: (
            "colmap.review_apply_pending_filters",
            "Apply Pending Filters",
        ),
        COLMAP_OT_review_clear_pending_filters: (
            "colmap.review_clear_pending_filters",
            "Clear Pending Filters",
        ),
    }
    for operator, (operator_id, label) in operator_contract.items():
        assert_true(
            (operator.bl_idname, operator.bl_label) == (operator_id, label),
            f"{operator.__name__}의 ID 또는 label이 잘못되었습니다.",
        )
    camera_review_layout = _RecordingLayout()
    camera_review_panel = type(
        "CameraReviewPanel",
        (),
        {"layout": camera_review_layout},
    )()
    camera_review_panel_class = next(
        panel
        for panel in PANEL_CLASSES
        if panel.bl_idname == "COLMAP_PT_camera_review"
    )
    camera_review_panel_class.draw(camera_review_panel, bpy.context)
    export_layout = _RecordingLayout()
    export_panel = type(
        "ExportPanel",
        (),
        {"layout": export_layout},
    )()
    export_panel_class = next(
        panel
        for panel in PANEL_CLASSES
        if panel.bl_idname == "COLMAP_PT_export"
    )
    export_panel_class.draw(export_panel, bpy.context)
    assert_true(
        export_layout.calls
        == [
            ("PROP", "export_output_format"),
            (
                "PROP_OPTIONS",
                "export_output_format",
                {"text": "Format"},
            ),
            ("PROP", "export_backup_enabled"),
            (
                "PROP_OPTIONS",
                "export_backup_enabled",
                {"text": "Backup"},
            ),
            ("OPERATOR", "colmap.export"),
        ],
        "Export 패널에 Format, Backup과 Export UI가 정확히 표시되지 않았습니다.",
    )
    assert_true(
        ("PROP", "review_frame_start") not in camera_review_layout.calls
        and (
            "OPERATOR",
            "colmap.review_build_timeline",
        )
        not in camera_review_layout.calls,
        "Camera Review 패널에 Frame Start 또는 수동 Timeline 버튼이 남아 있습니다.",
    )
    assert_true(
        (
            "OPERATOR",
            "colmap.review_restore_view",
        )
        not in camera_review_layout.calls
        and bpy.types.Operator.bl_rna_get_subclass_py(
            "COLMAP_OT_review_restore_view"
        )
        is None,
        "Restore Previous View UI 또는 오퍼레이터가 남아 있습니다.",
    )
    assert_true(
        (
            "OPERATOR",
            "colmap.review_previous",
        )
        not in camera_review_layout.calls
        and (
            "OPERATOR",
            "colmap.review_next",
        )
        not in camera_review_layout.calls,
        "Camera Review 패널에 Previous 또는 Next 버튼이 남아 있습니다.",
    )
    assert_true(
        ("OPERATOR", "colmap.review_undo_last_filter")
        in camera_review_layout.calls,
        "Camera Review 패널에 Undo Last Filter 버튼이 없습니다.",
    )
    property_options = {
        call[1]: call[2]
        for call in camera_review_layout.calls
        if len(call) == 3 and call[0] == "PROP_OPTIONS"
    }
    assert_true(
        property_options["review_background_alpha"].get("slider") is False
        and property_options["review_background_enabled"].get("toggle") is False,
        "Camera Review 배경 설정이 파란색 채움 컨트롤로 남아 있습니다.",
    )
    assert_true(
        all(
            not (
                call[1].startswith("VRAM Window:")
                or call[1] == settings.review_status
            )
            for call in camera_review_layout.calls
            if len(call) == 3 and call[0] == "LABEL"
        ),
        "Camera Review 하단에 VRAM Window 또는 Review 상태 문구가 남아 있습니다.",
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, images_directory = _make_review_fixture(root)
        scene_camera_before_import = scene.camera

        # 사용자가 먼저 연 Image는 애드온 관리 Image로 전환하면 안 됩니다.
        user_image = bpy.data.images.load(
            str(images_directory / "two.ppm"),
            check_existing=True,
        )
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        result = bpy.ops.colmap.import_model()
        assert_true(result == {"FINISHED"}, settings.status)

        session = current_review_session(bpy.context, strict=True)
        assert_true(
            tuple(entry.image_id for entry in session.review_index.entries)
            == (2, 5, 9, 12),
            "Review가 IMAGE_ID 오름차순으로 초기화되지 않았습니다.",
        )
        assert_true(
            (session.current_index, session.current_image_id) == (0, 2),
            "Import 성공 뒤 첫 생존 IMAGE_ID가 현재 Review가 아닙니다.",
        )
        assert_true(
            (
                settings.review_current_index,
                settings.review_current_image_id,
                settings.review_total_count,
            )
            == (0, "2", 4),
            "Review pair와 UI Property가 일관되지 않습니다.",
        )
        assert_true(
            scene.camera is session.camera_objects[2],
            "Import 자동 Timeline이 첫 Review Camera를 Scene.camera에 연결하지 못했습니다.",
        )
        assert_true(
            user_image.get(OWNER_KEY) != OWNER_TOKEN,
            "사용자가 연 Image에 애드온 소유권을 덧씌웠습니다.",
        )

        # Import 성공 경계에서 Timeline은 Frame 1부터 정확히 한 번 자동 생성됩니다.
        imported_manifest = json.loads(settings.review_marker_manifest_json)
        imported_entries = [
            item
            for item in imported_manifest
            if item["dataset_uuid"] == session.dataset_uuid
        ]
        assert_true(
            session.timeline_active
            and session.timeline_refresh_enabled
            and [item["frame"] for item in imported_entries] == [1, 2, 3, 4]
            and [int(item["image_id"]) for item in imported_entries]
            == [2, 5, 9, 12]
            and scene.frame_current == 1
            and scene.camera is session.camera_objects[2],
            "Import가 Camera Review Timeline을 Frame 1부터 자동 생성하지 못했습니다.",
        )

        # 실제 Marker는 세션 수명만 따르고 Manifest와 Camera Object는 영구 보존합니다.
        marker_manifest_before_detach = settings.review_marker_manifest_json
        camera_pointers_before_detach = tuple(
            camera.as_pointer() for camera in session.camera_objects.values()
        )
        assert_true(
            camera_review_timeline.detach_review_timeline_markers(session) == 4
            and not camera_review_timeline.modifier_review_markers_attached(scene)
            and settings.review_marker_manifest_json == marker_manifest_before_detach
            and tuple(camera.as_pointer() for camera in session.camera_objects.values())
            == camera_pointers_before_detach,
            "Timeline Detach가 Marker만 제거하거나 Manifest/Camera를 보존하지 못했습니다.",
        )
        previous_manifest_signature = tuple(
            (
                item["marker_name"],
                item["frame"],
                item["image_id"],
                item["camera"]["dataset_uuid"],
                item["camera"]["image_id"],
            )
            for item in imported_entries
        )
        camera_review_timeline.release_modifier_marker_session(scene)
        session.timeline_active = True
        session.timeline_refresh_enabled = True
        session.timeline_manifest_signature = previous_manifest_signature
        camera_review_timeline.attach_session(session)
        inactive_build_warnings = camera_review_timeline.build_or_refresh_timeline(
            bpy.context,
            session,
        )
        assert_true(
            inactive_build_warnings
            and not camera_review_timeline.modifier_review_markers_attached(scene)
            and camera_review_timeline.restore_moved_owned_markers(session) == (),
            "비활성 Modifier의 Handler 또는 Guard가 Marker를 다시 생성했습니다.",
        )
        camera_review_timeline.acquire_modifier_marker_session(
            scene,
            window=bpy.context.window,
        )
        assert_true(
            camera_review_timeline.attach_review_timeline_markers(session) == 4
            and camera_review_timeline.modifier_review_markers_attached(scene),
            "Timeline Attach가 Manifest의 Marker를 동일한 Camera 연결로 복원하지 못했습니다.",
        )
        for item in imported_entries:
            restored_marker = scene.timeline_markers[item["marker_name"]]
            assert_true(
                restored_marker.frame == item["frame"]
                and restored_marker.camera
                is session.camera_objects[int(item["image_id"])],
                "Timeline Attach가 name/frame/camera 계약을 복원하지 못했습니다.",
            )

        # 외부 Camera Marker 충돌은 보존하며 Attach를 원자적으로 차단합니다.
        camera_review_timeline.detach_review_timeline_markers(session)
        name_collision_marker = scene.timeline_markers.new(
            imported_entries[0]["marker_name"],
            frame=-100,
        )
        try:
            camera_review_timeline.attach_review_timeline_markers(session)
            name_collision_blocked = False
        except ValueError:
            name_collision_blocked = True
        assert_true(
            name_collision_blocked
            and scene.timeline_markers.get(name_collision_marker.name) is not None
            and not camera_review_timeline.modifier_review_markers_attached(scene),
            "사용자 Marker 이름 충돌을 보존·차단하지 못했습니다.",
        )
        scene.timeline_markers.remove(name_collision_marker)
        external_camera_data = bpy.data.cameras.new("사용자 외부 Camera Data")
        external_camera = bpy.data.objects.new("사용자 외부 Camera", external_camera_data)
        scene.collection.objects.link(external_camera)
        external_camera_marker = scene.timeline_markers.new(
            "사용자 외부 Camera Marker",
            frame=imported_entries[0]["frame"],
        )
        external_camera_marker.camera = external_camera
        try:
            camera_review_timeline.attach_review_timeline_markers(session)
            external_collision_blocked = False
        except ValueError:
            external_collision_blocked = True
        assert_true(
            external_collision_blocked
            and scene.timeline_markers.get(external_camera_marker.name)
            is not None
            and scene.timeline_markers.get(external_camera_marker.name).as_pointer()
            == external_camera_marker.as_pointer()
            and not camera_review_timeline.modifier_review_markers_attached(scene),
            "외부 Camera Marker 충돌을 보존·차단하지 못했습니다.",
        )
        scene.timeline_markers.remove(external_camera_marker)
        camera_review_timeline.attach_review_timeline_markers(session)

        # Detach 도중 일부 제거 뒤 실패하면 제거된 Marker 전체를 복원합니다.
        original_remove_exact_marker = camera_review_timeline._remove_exact_marker
        detach_remove_calls = [0]

        def fail_second_detach_remove(markers, marker, *, strict=False):
            """두 번째 Detach 제거에서 의도한 실패를 발생시킵니다."""
            detach_remove_calls[0] += 1
            if detach_remove_calls[0] == 2:
                raise RuntimeError("의도한 Timeline Detach 실패")
            return original_remove_exact_marker(markers, marker, strict=strict)

        camera_review_timeline._remove_exact_marker = fail_second_detach_remove
        try:
            try:
                camera_review_timeline.detach_review_timeline_markers(session)
                detach_rollback_succeeded = False
            except RuntimeError:
                detach_rollback_succeeded = True
        finally:
            camera_review_timeline._remove_exact_marker = original_remove_exact_marker
        assert_true(
            detach_rollback_succeeded
            and camera_review_timeline.modifier_review_markers_attached(scene)
            and settings.review_marker_manifest_json == marker_manifest_before_detach,
            "Timeline Detach 일부 실패가 Marker와 Manifest를 원상복구하지 못했습니다.",
        )
        user_marker = scene.timeline_markers.new("사용자 Marker", frame=-50)
        colliding_name = camera_review_timeline.marker_base_name(
            session.dataset_uuid,
            2,
        )
        collision_marker = scene.timeline_markers.new(colliding_name, frame=-49)
        foreign_camera = bpy.data.cameras.new("다른 Dataset Camera Data")
        foreign_object = bpy.data.objects.new("다른 Dataset Camera", foreign_camera)
        scene.collection.objects.link(foreign_object)
        foreign_object[OWNER_KEY] = OWNER_TOKEN
        foreign_object[RECORD_KIND_KEY] = "CAMERA"
        foreign_object[DATASET_UUID_KEY] = "foreign-dataset"
        foreign_object["colmap_image_id_text"] = "77"
        foreign_marker = scene.timeline_markers.new("다른 Dataset Marker", frame=-48)
        foreign_marker.camera = foreign_object
        settings.review_marker_manifest_json = json.dumps(
            [
                *imported_manifest,
                {
                    "dataset_uuid": "foreign-dataset",
                    "source_fingerprint": "foreign-source",
                    "edit_revision": 3,
                    "marker_name": foreign_marker.name,
                    "frame": foreign_marker.frame,
                    "image_id": "77",
                    "camera": {
                        "dataset_uuid": "foreign-dataset",
                        "image_id": "77",
                        "name": foreign_object.name,
                    },
                }
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert_true(
            bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
            settings.review_status,
        )

        # Timeline Build 기록 실패는 Marker와 manifest/session/playhead를 복구합니다.
        session = current_review_session(bpy.context, strict=True)
        build_failure_token = (
            settings.review_marker_manifest_json,
            session.timeline_active,
            session.timeline_refresh_enabled,
            session.timeline_manifest_signature,
            scene.frame_current,
            scene.camera,
            tuple(
                sorted(
                    (
                        marker.name,
                        marker.frame,
                        (
                            marker.camera.get(DATASET_UUID_KEY),
                            marker.camera.get("colmap_image_id_text"),
                            marker.camera.name,
                        )
                        if marker.camera is not None
                        else None,
                    )
                    for marker in scene.timeline_markers
                )
            ),
        )
        original_write_manifest = camera_review_timeline._write_manifest

        def fail_manifest_write(target_settings, _entries):
            target_settings.review_marker_manifest_json = '{"partial":true}'
            raise RuntimeError("의도한 Timeline manifest 기록 실패")

        camera_review_timeline._write_manifest = fail_manifest_write
        try:
            try:
                failed_build_result = bpy.ops.colmap.review_build_timeline()
            except RuntimeError:
                failed_build_result = {"CANCELLED"}
        finally:
            camera_review_timeline._write_manifest = original_write_manifest
        assert_true(
            failed_build_result == {"CANCELLED"}
            and build_failure_token
            == (
                settings.review_marker_manifest_json,
                session.timeline_active,
                session.timeline_refresh_enabled,
                session.timeline_manifest_signature,
                scene.frame_current,
                scene.camera,
                tuple(
                    sorted(
                        (
                            marker.name,
                            marker.frame,
                            (
                                marker.camera.get(DATASET_UUID_KEY),
                                marker.camera.get("colmap_image_id_text"),
                                marker.camera.name,
                            )
                            if marker.camera is not None
                            else None,
                        )
                        for marker in scene.timeline_markers
                    )
                ),
            ),
            "Timeline Build 실패가 기존 Marker/manifest/session/frame/Scene.camera를 복구하지 못했습니다.",
        )

        # Previous/Next는 활성 Timeline의 playhead와 양방향으로 동기화합니다.
        original_timeline_navigation_update = camera_review_images.update_working_set
        camera_review_images.update_working_set = lambda *_args, **_kwargs: ()
        try:
            assert_true(
                bpy.ops.colmap.review_next() == {"FINISHED"}
                and scene.frame_current == 2,
                "Next가 활성 Timeline playhead를 동기화하지 않았습니다.",
            )
            assert_true(
                bpy.ops.colmap.review_previous() == {"FINISHED"}
                and scene.frame_current == 1,
                "Previous가 활성 Timeline playhead를 동기화하지 않았습니다.",
            )
        finally:
            camera_review_images.update_working_set = original_timeline_navigation_update

        # frame handler는 O(1) target만 예약하고 timer 성공 뒤 pair를 확정합니다.
        working_set_calls: list[int] = []
        original_update_working_set = camera_review_images.update_working_set
        original_ensure_runtime = camera_review_session.ensure_runtime
        original_load_image = camera_review_images._load_image_datablock

        def record_working_set(_session, target_index):
            working_set_calls.append(target_index)
            return ()

        camera_review_images.update_working_set = record_working_set
        camera_review_session.ensure_runtime = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("frame handler가 ensure_runtime을 호출했습니다."))
        )
        camera_review_images._load_image_datablock = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("frame handler가 Image를 로드했습니다."))
        )
        try:
            previous_pair = (session.current_index, session.current_image_id)
            previous_scene_camera = scene.camera
            scene.frame_current = 3
            camera_review_timeline.frame_change_post(scene)
            session = current_review_session(bpy.context)
            assert_true(
                (session.current_index, session.current_image_id) == previous_pair
                and scene.camera is previous_scene_camera
                and not working_set_calls,
                "frame handler가 timer 성공 전에 current pair/Scene.camera를 변경했거나 작업 집합을 직접 갱신했습니다.",
            )
            camera_review_timeline.process_pending_timeline_updates()
            assert_true(
                working_set_calls == [2]
                and (session.current_index, session.current_image_id) == (2, 9)
                and scene.camera is session.camera_objects[9],
                "예약 callback 성공 뒤 Camera Review 작업 집합과 pair를 원자적으로 확정하지 않았습니다.",
            )
        finally:
            camera_review_images.update_working_set = original_update_working_set
            camera_review_session.ensure_runtime = original_ensure_runtime
            camera_review_images._load_image_datablock = original_load_image

        # scrub 대상 이미지 로드 실패는 마지막 성공 상태를 완전히 보존합니다.
        session = current_review_session(bpy.context, strict=True)
        scrub_failure_token = (
            session.current_index,
            session.current_image_id,
            scene.frame_current,
            scene.camera,
            tuple(sorted(session.working_set_image_ids)),
            tuple(sorted(session.resident_images)),
            tuple(sorted(session.background_slots)),
            settings.review_current_index,
            settings.review_current_image_id,
            settings.review_vram_window,
            settings.review_image_status,
        )
        original_ensure_managed_review_image = (
            camera_review_images.ensure_managed_review_image
        )

        def fail_scrub_target_image(_session, entry):
            if entry.image_id == 5:
                raise RuntimeError("의도한 Timeline scrub 이미지 로드 실패")
            return original_ensure_managed_review_image(_session, entry)

        camera_review_images.ensure_managed_review_image = fail_scrub_target_image
        try:
            scene.frame_set(2)
            camera_review_timeline.process_pending_timeline_updates()
        finally:
            camera_review_images.ensure_managed_review_image = (
                original_ensure_managed_review_image
            )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            scrub_failure_token
            == (
                session.current_index,
                session.current_image_id,
                scene.frame_current,
                scene.camera,
                tuple(sorted(session.working_set_image_ids)),
                tuple(sorted(session.resident_images)),
                tuple(sorted(session.background_slots)),
                settings.review_current_index,
                settings.review_current_image_id,
                settings.review_vram_window,
                settings.review_image_status,
            ),
            "Timeline scrub 이미지 로드 실패가 마지막 성공 pair/Camera/frame/작업 집합/background를 변경했습니다.",
        )

        scene.frame_set(1)
        camera_review_timeline.process_pending_timeline_updates()
        session = current_review_session(bpy.context, strict=True)
        manifest = json.loads(settings.review_marker_manifest_json)
        current_entries = [
            item for item in manifest
            if item["dataset_uuid"] == session.dataset_uuid
        ]
        assert_true(
            tuple(item["image_id"] for item in current_entries)
            == ("2", "5", "9", "12")
            and tuple(item["frame"] for item in current_entries)
            == (1, 2, 3, 4),
            "Timeline manifest가 IMAGE_ID 오름차순 연속 frame이 아닙니다.",
        )
        for item in current_entries:
            marker = scene.timeline_markers[item["marker_name"]]
            image_id = int(item["image_id"])
            assert_true(
                marker.frame == item["frame"]
                and marker.camera is session.camera_objects[image_id],
                "Timeline Marker의 frame 또는 Camera binding이 잘못되었습니다.",
            )
        assert_true(
            scene.timeline_markers.get(user_marker.name) is not None
            and scene.timeline_markers.get(user_marker.name).frame == -50
            and scene.timeline_markers.get(collision_marker.name) is not None
            and scene.timeline_markers.get(collision_marker.name).frame == -49
            and scene.timeline_markers.get(foreign_marker.name) is not None
            and scene.timeline_markers.get(foreign_marker.name).frame == -48
            and scene.timeline_markers.get(foreign_marker.name).camera is foreign_object
            and current_entries[0]["marker_name"] != colliding_name,
            "사용자/다른 Dataset Marker 보존 또는 이름 충돌 처리가 잘못되었습니다.",
        )

        # 사용자가 이동한 현재 Dataset Camera Marker만 원래 Frame으로 복구합니다.
        protected_entry = current_entries[0]
        protected_marker = scene.timeline_markers[
            protected_entry["marker_name"]
        ]
        expected_protected_frame = protected_marker.frame
        expected_review_camera = session.camera_objects[session.current_image_id]
        protected_marker.select = True
        protected_marker.frame = expected_protected_frame + 100
        user_marker.frame = -47
        foreign_marker.frame = -46
        scene.camera = session.camera_objects[5]
        restored_marker_names = (
            camera_review_timeline.restore_moved_owned_markers(session)
        )
        assert_true(
            restored_marker_names == (protected_marker.name,)
            and protected_marker.frame == expected_protected_frame
            and not protected_marker.select
            and user_marker.frame == -47
            and foreign_marker.frame == -46
            and scene.camera is expected_review_camera
            and "move cancelled" in settings.review_status,
            "COLMAP Camera Marker 이동 취소가 소유 Marker만 복구하거나 Review Camera를 재동기화하지 못했습니다.",
        )
        user_marker.frame = -50
        foreign_marker.frame = -48

        marker_signature = tuple(
            sorted(
                (
                    marker.name,
                    marker.frame,
                    marker.camera.as_pointer() if marker.camera else 0,
                )
                for marker in scene.timeline_markers
            )
        )
        assert_true(
            bpy.ops.colmap.review_build_timeline() == {"FINISHED"}
            and marker_signature
            == tuple(
                sorted(
                    (
                        marker.name,
                        marker.frame,
                        marker.camera.as_pointer() if marker.camera else 0,
                    )
                    for marker in scene.timeline_markers
                )
            ),
            "Timeline 반복 Build가 멱등적이지 않습니다.",
        )
        assert_true(
            scene.frame_current == 1
            and scene.camera is session.camera_objects[2],
            "Timeline Build 뒤 현재 Review frame과 Scene.camera가 동기화되지 않았습니다.",
        )

        # 손상 manifest는 기존 Marker를 삭제하지 않고 경고와 함께 재구성합니다.
        damaged_marker_names = {marker.name for marker in scene.timeline_markers}
        settings.review_marker_manifest_json = "{broken"
        assert_true(
            bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
            settings.review_status,
        )
        assert_true(
            damaged_marker_names <= {marker.name for marker in scene.timeline_markers}
            and "manifest" in settings.review_status.lower(),
            "손상 manifest 처리에서 기존 Marker가 삭제되었거나 경고가 없습니다.",
        )

        # 음수/큰 Frame Start에서도 연속 frame과 O(1) frame map을 유지합니다.
        for frame_start in (-100, 100_000):
            settings.review_frame_start = frame_start
            assert_true(
                bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
                settings.review_status,
            )
            session = current_review_session(bpy.context, strict=True)
            assert_true(
                tuple(session.review_index.frame_to_image_id.items())
                == tuple(
                    (frame_start + index, image_id)
                    for index, image_id in enumerate((2, 5, 9, 12))
                ),
                "음수 또는 큰 Frame Start의 frame map이 잘못되었습니다.",
            )
        settings.review_frame_start = 1
        assert_true(
            bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
            settings.review_status,
        )

        rebuild_count = [0]
        original_build_index = camera_review_session._build_index

        def count_build_index(*args, **kwargs):
            rebuild_count[0] += 1
            return original_build_index(*args, **kwargs)

        camera_review_session._build_index = count_build_index
        try:
            session.edit_revision = -1
            session = ensure_review_session(bpy.context)
            assert_true(
                rebuild_count[0] == 1 and session.current_image_id == 2,
                "stale revision을 한 번만 재구성하거나 같은 IMAGE_ID를 유지하지 못했습니다.",
            )
            session.camera_object_pointers = ()
            session = ensure_review_session(bpy.context)
            assert_true(
                rebuild_count[0] == 2 and session.current_image_id == 2,
                "stale Camera map을 현재 Runtime에서 한 번만 재구성하지 못했습니다.",
            )
        finally:
            camera_review_session._build_index = original_build_index

        owned = managed_review_images(session.dataset_uuid)
        assert_true(
            len(owned) == 2
            and {image["colmap_review_image_id"] for image in owned} == {"2", "5"},
            "초기 작업 집합이 current/current+1 두 장으로 지연 로드되지 않았습니다.",
        )
        for image in owned:
            assert_true(
                image.get(RECORD_KIND_KEY) == REVIEW_IMAGE_KIND
                and image.get(DATASET_UUID_KEY) == session.dataset_uuid
                and image.get(SOURCE_FINGERPRINT_KEY)
                == session.source_fingerprint
                and image.get("colmap_review_source_path")
                and image.get("colmap_review_preview_scale") == 1,
                "관리 Review Image 소유권 metadata가 불완전합니다.",
            )

        cameras = managed_camera_map(bpy.context, session.dataset_uuid)
        user_slot = cameras[2].data.background_images.new()
        user_slot.image = user_image
        user_slot.show_background_image = False
        user_slot.alpha = 0.73
        managed_slots = tuple(session.background_slots.values())
        assert_true(
            managed_slots
            and all(
                slot.display_depth == "FRONT"
                and slot.show_background_image
                and abs(slot.alpha - 1.0) < 1.0e-6
                for slot in managed_slots
            ),
            "관리 배경 이미지가 FRONT 깊이, 표시 활성화와 기본 Alpha 1.0으로 생성되지 않았습니다.",
        )
        settings.review_background_alpha = 0.35
        assert_true(
            all(abs(slot.alpha - 0.35) < 1.0e-6 for slot in managed_slots)
            and abs(user_slot.alpha - 0.73) < 1.0e-6,
            "Camera Review Alpha 변경이 관리 slot에만 실시간 반영되지 않았습니다.",
        )
        settings.review_background_enabled = False
        assert_true(
            all(
                slot.display_depth == "FRONT"
                and not slot.show_background_image
                for slot in managed_slots
            )
            and not user_slot.show_background_image,
            "Camera Review 표시 끄기가 관리 slot에만 실시간 반영되지 않았습니다.",
        )
        settings.review_background_enabled = True
        assert_true(
            all(
                slot.display_depth == "FRONT"
                and slot.show_background_image
                for slot in managed_slots
            )
            and not user_slot.show_background_image,
            "Camera Review 표시 켜기가 관리 slot에만 실시간 반영되지 않았습니다.",
        )

        freed_image_ids: list[str] = []
        original_free = camera_review_images._free_image_gpu

        def record_free(image):
            freed_image_ids.append(image["colmap_review_image_id"])

        camera_review_images._free_image_gpu = record_free
        try:
            original_load = camera_review_images._load_image_datablock

            def fail_nine(path):
                if Path(path).name == "nine.ppm":
                    raise RuntimeError("의도한 현재 이미지 로드 실패")
                return original_load(path)

            camera_review_images._load_image_datablock = fail_nine
            assert_true(
                bpy.ops.colmap.review_next() == {"FINISHED"},
                "첫 Next 실행이 실패했습니다.",
            )
            session = current_review_session(bpy.context, strict=True)
            assert_true(
                session.working_set_image_ids == {2, 5, 9}
                and len(session.working_set_image_ids) <= 3,
                "중간 Camera 작업 집합이 current-1/current/current+1이 아닙니다.",
            )
            try:
                failed_navigation = bpy.ops.colmap.review_next()
            except RuntimeError:
                failed_navigation = {"CANCELLED"}
            assert_true(
                failed_navigation == {"CANCELLED"},
                "현재 이미지 로드 실패가 Camera Review 전환을 취소하지 않았습니다.",
            )
            session = current_review_session(bpy.context, strict=True)
            assert_true(
                (session.current_index, session.current_image_id) == (1, 5)
                and scene.camera is session.camera_objects[5],
                "이미지 로드 실패가 현재 pair 또는 Scene.camera를 중간 상태로 남겼습니다.",
            )
            camera_review_images._load_image_datablock = original_load
            assert_true(
                bpy.ops.colmap.review_next() == {"FINISHED"},
                "두 번째 Next 실행이 실패했습니다.",
            )
        finally:
            camera_review_images._load_image_datablock = original_load
            camera_review_images._free_image_gpu = original_free

        session = current_review_session(bpy.context, strict=True)
        assert_true(
            (session.current_index, session.current_image_id) == (2, 9),
            "Next가 Core clamp helper 결과를 Review pair에 반영하지 않았습니다.",
        )
        assert_true("2" in freed_image_ids, "작업 집합 밖 Image에 gl_free가 호출되지 않았습니다.")
        assert_true(
            user_slot.image is user_image and not user_slot.show_background_image,
            "기존 사용자 background slot이 변경되었습니다.",
        )
        camera_two_managed_slots = [
            slot
            for slot in cameras[2].data.background_images
            if slot.image is not None
            and slot.image.get(RECORD_KIND_KEY) == REVIEW_IMAGE_KIND
        ]
        assert_true(
            not camera_two_managed_slots,
            "eviction된 Image가 관리 background slot에서 계속 참조됩니다.",
        )

        assert_true(
            bpy.ops.colmap.review_next() == {"FINISHED"},
            "비지원 Camera pose로 이동하지 못했습니다.",
        )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            session.current_image_id == 12
            and not settings.review_exact_projection
            and settings.review_camera_model == "OPENCV",
            "비지원 Camera 모델이 정확 투영으로 표시되었습니다.",
        )
        unsupported_slots = [
            slot
            for slot in cameras[12].data.background_images
            if slot.image is not None
            and slot.image.get(RECORD_KIND_KEY) == REVIEW_IMAGE_KIND
        ]
        assert_true(
            not unsupported_slots,
            "비지원 Camera 모델에 관리 background가 표시되었습니다.",
        )
        assert_true(
            not COLMAP_OT_review_next.poll(bpy.context),
            "마지막 Camera에서 Next UI가 비활성화되지 않았습니다.",
        )
        clamped_session = navigate_review(bpy.context, "NEXT")
        assert_true(
            (clamped_session.current_index, clamped_session.current_image_id)
            == (3, 12),
            "마지막 Camera의 Python 탐색 호출이 Clamp되지 않았습니다.",
        )

        # 편집 모드 플래그가 켜진 동안 모든 Review 변경 오퍼레이터를 거부합니다.
        settings.camera_edit_mode_active = True
        assert_true(
            not COLMAP_OT_review_previous.poll(bpy.context),
            "Camera Edit Mode에서 Previous poll이 허용되었습니다.",
        )
        settings.camera_edit_mode_active = False

        assert_true(
            bpy.ops.colmap.review_previous() == {"FINISHED"},
            "비지원 Camera에서 Previous로 복귀하지 못했습니다.",
        )
        session = current_review_session(bpy.context, strict=True)
        primary_context = _ViewportContext(scene)
        secondary_context = _ViewportContext(scene)
        sidebar_context = _ViewportContext(scene, region_type="UI")
        assert_true(
            COLMAP_OT_review_look_through.poll(sidebar_context),
            "N 패널의 UI region에서 Look Through 버튼이 비활성화되었습니다.",
        )
        look_through_current(sidebar_context, session)
        sidebar_snapshot = next(iter(session.viewport_snapshots.values()))
        assert_true(
            sidebar_snapshot.region_pointer
            == sidebar_context.window_region.as_pointer()
            and sidebar_context.space_data.region_3d.view_perspective == "CAMERA",
            "N 패널 실행이 같은 Area의 WINDOW region Viewport를 사용하지 않았습니다.",
        )
        # 사용자 Restore 오퍼레이터는 제거했지만 내부 snapshot 복구를 검증합니다.
        restore_previous_view(sidebar_context, session)
        original_render = (
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.pixel_aspect_x,
            scene.render.pixel_aspect_y,
            scene.camera,
        )
        look_through_current(primary_context, session)
        expected_pixel_aspect = cameras[9].data.get(
            "colmap_pixel_aspect_y_over_x"
        )
        assert_true(
            expected_pixel_aspect is not None,
            "정확 모델 Camera data에 pixel aspect metadata가 없습니다.",
        )
        assert_true(
            primary_context.space_data.region_3d.view_perspective == "CAMERA"
            and scene.camera is cameras[9],
            "현재 Viewport와 Scene.camera가 Review Camera로 전환되지 않았습니다.",
        )
        assert_true(
            secondary_context.space_data.region_3d.view_perspective == "PERSP",
            "다른 VIEW_3D Viewport가 함께 변경되었습니다.",
        )
        applied_render = (
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.pixel_aspect_x,
            scene.render.pixel_aspect_y,
        )
        assert_true(
            applied_render[:4]
            == (640, 480, 100, 1.0)
            and abs(
                scene.render.pixel_aspect_y
                / scene.render.pixel_aspect_x
                - expected_pixel_aspect
            )
            < 1.0e-6,
            f"Look Through Render Aspect가 잘못되었습니다: {applied_render}",
        )
        assert_true(settings.review_restore_available, "Restore 가능 상태가 UI에 반영되지 않았습니다.")
        restore_previous_view(primary_context, session)
        assert_true(
            primary_context.space_data.region_3d.view_perspective == "PERSP"
            and (
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
                scene.render.pixel_aspect_x,
                scene.render.pixel_aspect_y,
                scene.camera,
            )
            == original_render,
            "동일 Viewport와 Render/Scene Camera snapshot이 복원되지 않았습니다.",
        )

        cameras[9].data["colmap_pixel_aspect_y_over_x"] = 0.5
        look_through_current(primary_context, session)
        assert_true(
            scene.render.pixel_aspect_x >= 1.0
            and scene.render.pixel_aspect_y >= 1.0
            and abs(
                scene.render.pixel_aspect_y
                / scene.render.pixel_aspect_x
                - 0.5
            )
            < 1.0e-6,
            "fx/fy < 1인 Camera의 Render pixel aspect 비율이 보존되지 않았습니다.",
        )
        restore_previous_view(primary_context, session)

        for invalid_ratio in (0.0, -1.0, float("inf"), float("nan")):
            cameras[9].data["colmap_pixel_aspect_y_over_x"] = invalid_ratio
            before_invalid_render = (
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
                scene.render.pixel_aspect_x,
                scene.render.pixel_aspect_y,
                scene.camera,
            )
            try:
                look_through_current(primary_context, session)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"잘못된 pixel aspect ratio가 거부되지 않았습니다: {invalid_ratio}"
                )
            assert_true(
                (
                    scene.render.resolution_x,
                    scene.render.resolution_y,
                    scene.render.resolution_percentage,
                    scene.render.pixel_aspect_x,
                    scene.render.pixel_aspect_y,
                    scene.camera,
                )
                == before_invalid_render
                and not session.viewport_snapshots,
                "잘못된 pixel aspect ratio가 Viewport 또는 Render 중간 상태를 남겼습니다.",
            )

        cameras[9].data[
            "colmap_pixel_aspect_y_over_x"
        ] = expected_pixel_aspect

        # 닫힌 Viewport만 남으면 새 Camera override를 적용하지 않고 이전 상태를 복원합니다.
        look_through_current(primary_context, session)
        timeline_was_active = session.timeline_active
        session.timeline_active = False
        primary_context.area._pointer = 0
        assert_true(
            bpy.ops.colmap.review_next() == {"FINISHED"},
            "무효 Viewport 정리 경계의 Next가 실패했습니다.",
        )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            not session.viewport_snapshots
            and session.render_snapshot is None
            and not settings.review_restore_available
            and (
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
                scene.render.pixel_aspect_x,
                scene.render.pixel_aspect_y,
                scene.camera,
            )
            == original_render,
            "닫힌 Viewport 뒤 Review Render/Scene Camera override가 남았습니다.",
        )
        session.timeline_active = timeline_was_active
        assert_true(
            bpy.ops.colmap.review_previous() == {"FINISHED"},
            "무효 Viewport 정리 뒤 원래 Camera로 복귀하지 못했습니다.",
        )
        session = current_review_session(bpy.context, strict=True)
        primary_context = _ViewportContext(scene)

        # 사용자가 Review 적용값을 바꾸면 Restore가 그 값을 덮어쓰지 않습니다.
        look_through_current(primary_context, session)
        scene.render.resolution_x = 777
        restore_previous_view(primary_context, session)
        assert_true(
            scene.render.resolution_x == 777
            and "사용자 변경" in settings.review_status,
            "Review 이후 사용자 Render 변경을 Restore가 덮어썼습니다.",
        )

        managed_count_before_revisit = len(managed_review_images(session.dataset_uuid))
        assert_true(
            bpy.ops.colmap.review_previous() == {"FINISHED"}
            and bpy.ops.colmap.review_previous() == {"FINISHED"},
            "eviction된 첫 Camera로 재접근하지 못했습니다.",
        )
        session = current_review_session(bpy.context, strict=True)
        revisited_slots = [
            slot
            for slot in cameras[2].data.background_images
            if slot.image is not None
            and slot.image.get(RECORD_KIND_KEY) == REVIEW_IMAGE_KIND
        ]
        assert_true(
            session.current_image_id == 2
            and len(managed_review_images(session.dataset_uuid))
            == managed_count_before_revisit
            and len(revisited_slots) == 1,
            "eviction Image 재접근 시 관리 datablock을 재사용하지 못했습니다.",
        )

        # 관리 Image를 사용자가 별도 slot에서 참조해도 애드온 slot로 오인하지 않습니다.
        review_image_two = revisited_slots[0].image
        user_managed_image_slot = cameras[2].data.background_images.new()
        user_managed_image_slot.image = review_image_two
        user_managed_image_slot.show_background_image = False
        user_managed_image_slot_pointer = user_managed_image_slot.as_pointer()
        assert_true(
            bpy.ops.colmap.review_next() == {"FINISHED"}
            and bpy.ops.colmap.review_next() == {"FINISHED"},
            "사용자 관리 Image slot eviction 위치로 이동하지 못했습니다.",
        )
        assert_true(
            any(
                slot.as_pointer() == user_managed_image_slot_pointer
                and slot.image is review_image_two
                and not slot.show_background_image
                for slot in cameras[2].data.background_images
            ),
            "사용자가 만든 managed Image background slot을 애드온이 삭제했습니다.",
        )
        assert_true(
            bpy.ops.colmap.review_previous() == {"FINISHED"}
            and bpy.ops.colmap.review_previous() == {"FINISHED"},
            "사용자 managed Image slot 검사 뒤 첫 Camera로 복귀하지 못했습니다.",
        )
        session = current_review_session(bpy.context, strict=True)

        # 작업 집합 준비 뒤 Viewport 확정이 실패해도 마지막 성공 상태를 복구합니다.
        viewport_failure_token = (
            session.current_index,
            session.current_image_id,
            scene.frame_current,
            scene.camera,
            tuple(sorted(session.working_set_image_ids)),
            tuple(sorted(session.resident_images)),
            tuple(sorted(session.background_slots)),
            settings.review_current_index,
            settings.review_current_image_id,
            settings.review_vram_window,
            settings.review_image_status,
        )
        original_update_review_view = (
            camera_review_viewport.update_review_view_for_current
        )
        viewport_failure_count = [0]

        def fail_first_viewport_commit(context, review_session):
            viewport_failure_count[0] += 1
            if viewport_failure_count[0] == 1:
                raise RuntimeError("의도한 Timeline Viewport 확정 실패")
            return original_update_review_view(context, review_session)

        camera_review_viewport.update_review_view_for_current = (
            fail_first_viewport_commit
        )
        try:
            scene.frame_current = 2
            camera_review_timeline.frame_change_post(scene)
            camera_review_timeline.process_pending_timeline_updates()
        finally:
            camera_review_viewport.update_review_view_for_current = (
                original_update_review_view
            )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            viewport_failure_token
            == (
                session.current_index,
                session.current_image_id,
                scene.frame_current,
                scene.camera,
                tuple(sorted(session.working_set_image_ids)),
                tuple(sorted(session.resident_images)),
                tuple(sorted(session.background_slots)),
                settings.review_current_index,
                settings.review_current_image_id,
                settings.review_vram_window,
                settings.review_image_status,
            ),
            "Timeline Viewport 확정 실패가 마지막 성공 상태를 복구하지 못했습니다.",
        )

        # 버튼 탐색도 Viewport 확정 실패 시 같은 원자성 계약을 지킵니다.
        navigation_failure_token = (
            session.current_index,
            session.current_image_id,
            scene.frame_current,
            scene.camera,
            tuple(sorted(session.working_set_image_ids)),
            tuple(sorted(session.resident_images)),
            tuple(sorted(session.background_slots)),
            settings.review_current_index,
            settings.review_current_image_id,
            settings.review_vram_window,
            settings.review_image_status,
        )
        navigation_failure_count = [0]

        def fail_first_navigation_view(context, review_session):
            navigation_failure_count[0] += 1
            if navigation_failure_count[0] == 1:
                raise RuntimeError("의도한 Camera Review Viewport 확정 실패")
            return original_update_review_view(context, review_session)

        camera_review_viewport.update_review_view_for_current = (
            fail_first_navigation_view
        )
        try:
            try:
                navigation_result = bpy.ops.colmap.review_next()
            except RuntimeError:
                navigation_result = {"CANCELLED"}
        finally:
            camera_review_viewport.update_review_view_for_current = (
                original_update_review_view
            )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            navigation_result == {"CANCELLED"}
            and navigation_failure_token
            == (
                session.current_index,
                session.current_image_id,
                scene.frame_current,
                scene.camera,
                tuple(sorted(session.working_set_image_ids)),
                tuple(sorted(session.resident_images)),
                tuple(sorted(session.background_slots)),
                settings.review_current_index,
                settings.review_current_image_id,
                settings.review_vram_window,
                settings.review_image_status,
            ),
            "Previous/Next Viewport 확정 실패가 마지막 성공 상태를 복구하지 못했습니다.",
        )

        managed_count_before_reload = len(managed_review_images(session.dataset_uuid))
        result = bpy.ops.colmap.import_model()
        assert_true(result == {"FINISHED"}, settings.status)
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            len(managed_review_images(session.dataset_uuid)) == managed_count_before_reload,
            "같은 Dataset/IMAGE_ID/경로의 관리 Image가 중복 생성되었습니다.",
        )
        assert_true(
            (session.current_index, session.current_image_id) == (0, 2),
            "재Import 성공 뒤 첫 생존 IMAGE_ID로 Review가 갱신되지 않았습니다.",
        )

        # Filter 등록은 권위 모델을 바꾸지 않고 Review·Viewport·Timeline만 제외합니다.
        assert_true(
            bpy.ops.colmap.review_build_timeline() == {"FINISHED"}
            and bpy.ops.colmap.review_next() == {"FINISHED"},
            settings.review_status,
        )
        runtime_before_queue = ensure_runtime(scene)
        source_bytes = {
            path.name: path.read_bytes()
            for path in (root / "sparse" / "0").iterdir()
            if path.is_file()
        }
        manifest_before_filter = json.loads(settings.review_marker_manifest_json)
        removed_marker_name = next(
            item["marker_name"]
            for item in manifest_before_filter
            if item["dataset_uuid"] == session.dataset_uuid
            and item["image_id"] == "5"
        )
        second_marker_name = next(
            item["marker_name"]
            for item in manifest_before_filter
            if item["dataset_uuid"] == session.dataset_uuid
            and item["image_id"] == "2"
        )
        queue_failure_pair = (
            session.current_index,
            session.current_image_id,
            scene.frame_current,
            scene.camera,
        )
        original_remove_pending_marker = (
            camera_review_timeline.remove_pending_camera_marker
        )
        camera_review_timeline.remove_pending_camera_marker = (
            lambda *_args, **_kwargs: (
                (_ for _ in ()).throw(
                    RuntimeError("의도한 Pending Marker 제거 실패")
                )
            )
        )
        try:
            try:
                failed_queue = bpy.ops.colmap.review_filter_current()
            except RuntimeError:
                failed_queue = {"CANCELLED"}
        finally:
            camera_review_timeline.remove_pending_camera_marker = (
                original_remove_pending_marker
            )
        session = current_review_session(bpy.context, strict=True)
        failed_queue_point = require_managed_point_object(
            bpy.context,
            runtime_before_queue.dataset_uuid,
        )
        assert_true(
            failed_queue == {"CANCELLED"}
            and (
                session.current_index,
                session.current_image_id,
                scene.frame_current,
                scene.camera,
            )
            == queue_failure_pair
            and settings.review_pending_filter_json == "{}"
            and settings.review_pending_filter_order_json == "[]"
            and not session.pending_image_ids
            and not session.camera_objects[5].hide_get()
            and not any(
                item.value
                for item in failed_queue_point.data.attributes[
                    "colmap_pending_filtered"
                ].data
            )
            and scene.timeline_markers.get(removed_marker_name) is not None,
            "Pending 등록 실패가 Review pair, 표시 또는 Timeline을 복구하지 못했습니다.",
        )
        assert_true(
            bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        session = current_review_session(bpy.context, strict=True)
        point_object = require_managed_point_object(
            bpy.context,
            runtime_before_queue.dataset_uuid,
        )
        pending_attribute = point_object.data.attributes["colmap_pending_filtered"]
        pending_flags = {
            point_id: pending_attribute.data[index].value
            for index, point_id in enumerate(runtime_before_queue.point_id_order)
        }
        assert_true(
            settings.edit_revision == 0
            and ensure_runtime(scene) is runtime_before_queue
            and set(runtime_before_queue.current_reconstruction.images)
            == {2, 5, 9, 12}
            and set(runtime_before_queue.current_reconstruction.points3d)
            == {100, 200, 300}
            and settings.review_pending_camera_count == 1
            and settings.review_pending_point_count == 2
            and session.pending_image_ids == frozenset({5})
            and session.current_image_id == 9
            and session.camera_objects[5].hide_get()
            and not pending_flags[100]
            and pending_flags[200]
            and pending_flags[300]
            and scene.timeline_markers.get(removed_marker_name) is None,
            "Filter 등록이 권위 모델을 보존한 채 Pending overlay를 만들지 못했습니다.",
        )
        assert_true(
            source_bytes
            == {
                path.name: path.read_bytes()
                for path in (root / "sparse" / "0").iterdir()
                if path.is_file()
            },
            "Filter 등록이 원본 sparse 파일 bytes를 변경했습니다.",
        )

        # 추가 순서가 IMAGE_ID 정렬과 달라도 마지막으로 누른 Camera만 복원합니다.
        assert_true(
            bpy.ops.colmap.review_previous() == {"FINISHED"}
            and session.current_image_id == 2
            and bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        assert_true(
            settings.review_pending_camera_count == 2
            and json.loads(settings.review_pending_filter_order_json) == [5, 2]
            and session.pending_image_ids == frozenset({2, 5})
            and session.camera_objects[2].hide_get()
            and scene.timeline_markers.get(second_marker_name) is None,
            "Pending 추가 순서 또는 두 번째 Camera 표시가 올바르지 않습니다.",
        )
        undo_failure_pair = (
            session.current_index,
            session.current_image_id,
            scene.frame_current,
            scene.camera,
        )
        original_undo_timeline_refresh = (
            camera_review_timeline.build_or_refresh_timeline
        )
        camera_review_timeline.build_or_refresh_timeline = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("의도한 Pending Undo Timeline 실패")
            )
        )
        try:
            try:
                failed_pending_undo = bpy.ops.colmap.review_undo_last_filter()
            except RuntimeError:
                failed_pending_undo = {"CANCELLED"}
        finally:
            camera_review_timeline.build_or_refresh_timeline = (
                original_undo_timeline_refresh
            )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            failed_pending_undo == {"CANCELLED"}
            and settings.review_pending_camera_count == 2
            and json.loads(settings.review_pending_filter_order_json) == [5, 2]
            and session.pending_image_ids == frozenset({2, 5})
            and (
                session.current_index,
                session.current_image_id,
                scene.frame_current,
                scene.camera,
            )
            == undo_failure_pair
            and session.camera_objects[2].hide_get()
            and session.camera_objects[5].hide_get()
            and scene.timeline_markers.get(second_marker_name) is None
            and scene.timeline_markers.get(removed_marker_name) is None,
            "Undo Last Filter 실패가 Pending 표시·Review·Timeline을 복구하지 못했습니다.",
        )
        assert_true(
            bpy.ops.colmap.review_undo_last_filter() == {"FINISHED"},
            settings.review_status,
        )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            settings.edit_revision == 0
            and ensure_runtime(scene) is runtime_before_queue
            and settings.review_pending_camera_count == 1
            and json.loads(settings.review_pending_filter_order_json) == [5]
            and session.pending_image_ids == frozenset({5})
            and session.current_image_id == 2
            and not session.camera_objects[2].hide_get()
            and session.camera_objects[5].hide_get()
            and scene.timeline_markers.get(second_marker_name) is not None
            and scene.timeline_markers.get(removed_marker_name) is None,
            "Undo Last Filter가 마지막 추가 Camera와 Review·Timeline을 복원하지 못했습니다.",
        )
        try:
            blocked_import = bpy.ops.colmap.import_model()
        except RuntimeError:
            blocked_import = {"CANCELLED"}
        assert_true(
            blocked_import == {"CANCELLED"},
            "Pending 상태에서 Import가 허용되었습니다.",
        )

        # Clear는 데이터와 revision을 유지하면서 표시와 Timeline을 복원합니다.
        assert_true(
            bpy.ops.colmap.review_clear_pending_filters() == {"FINISHED"}
            and bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
            settings.review_status,
        )
        assert_true(
            settings.edit_revision == 0
            and settings.review_pending_filter_json == "{}"
            and settings.review_pending_filter_order_json == "[]"
            and not session.pending_image_ids
            and not session.camera_objects[5].hide_get()
            and not any(item.value for item in pending_attribute.data)
            and scene.timeline_markers.get(removed_marker_name) is not None,
            "Clear가 Pending 표시와 Timeline을 복원하지 못했습니다.",
        )

        # native Undo처럼 저장 JSON만 비워진 상태 변경도 orphan overlay를 정리합니다.
        orphan_target_image_id = session.current_image_id
        orphan_previous_index = settings.review_current_index
        assert_true(
            orphan_target_image_id is not None
            and bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        orphan_camera = session.camera_objects[orphan_target_image_id]
        settings.review_pending_filter_json = "{}"
        settings.review_current_image_id = str(orphan_target_image_id)
        settings.review_current_index = orphan_previous_index
        state_handlers.invalidate_transient_state()
        assert_true(
            not orphan_camera.hide_get()
            and not any(item.value for item in pending_attribute.data)
            and settings.review_pending_camera_count == 0
            and settings.review_pending_point_count == 0
            and settings.review_pending_filter_status
            == camera_filter_queue.PENDING_STATUS_EMPTY,
            "상태 무효화가 JSON에서 제거된 orphan Pending overlay를 정리하지 못했습니다.",
        )
        runtime_before_queue = ensure_runtime(scene)
        session = ensure_review_session(bpy.context)

        # 저장 JSON은 Runtime·Review cache가 사라진 뒤에도 token 검증 후 복원됩니다.
        move_for_restore_test = (
            bpy.ops.colmap.review_next
            if settings.review_current_index == 0
            else bpy.ops.colmap.review_previous
        )
        assert_true(
            move_for_restore_test() == {"FINISHED"}
            and bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        saved_pending_json = settings.review_pending_filter_json
        saved_pending_order_json = settings.review_pending_filter_order_json
        camera_review_session.clear_review_sessions(bpy.context)
        camera_filter_queue.clear_pending_runtime()
        clear_runtime()
        restored_runtime = ensure_runtime(scene)
        restored_session = ensure_review_session(bpy.context)
        restored_pending = camera_filter_queue.restore_pending_filters(
            bpy.context,
            state=restored_runtime,
            session=restored_session,
        )
        assert_true(
            restored_pending is not None
            and settings.review_pending_filter_json == saved_pending_json
            and settings.review_pending_filter_order_json
            == saved_pending_order_json
            and restored_session.pending_image_ids == frozenset({5})
            and restored_session.camera_objects[5].hide_get(),
            "저장 Pending JSON에서 overlay를 복원하지 못했습니다.",
        )
        settings.review_pending_filter_status = (
            camera_filter_queue.PENDING_STATUS_STALE
        )
        assert_true(
            not COLMAP_OT_review_filter_current.poll(bpy.context),
            "stale Pending 상태에서 추가 Filter가 활성화되었습니다.",
        )
        assert_true(
            not COLMAP_OT_review_undo_last_filter.poll(bpy.context),
            "stale Pending 상태에서 Undo Last Filter가 활성화되었습니다.",
        )
        original_clear_all_point_flags = (
            camera_filter_queue._clear_all_point_flags
        )

        def clear_flags_then_report_failure(context, dataset_uuid):
            original_clear_all_point_flags(context, dataset_uuid)
            raise RuntimeError("의도한 stale Point 표시 정리 경고")

        camera_filter_queue._clear_all_point_flags = (
            clear_flags_then_report_failure
        )
        try:
            stale_clear_result = bpy.ops.colmap.review_clear_pending_filters()
        finally:
            camera_filter_queue._clear_all_point_flags = (
                original_clear_all_point_flags
            )
        assert_true(
            stale_clear_result == {"FINISHED"}
            and settings.review_pending_filter_json == "{}"
            and settings.review_pending_filter_order_json == "[]"
            and settings.review_pending_filter_status
            == camera_filter_queue.PENDING_STATUS_EMPTY
            and not restored_session.pending_image_ids
            and not restored_session.camera_objects[5].hide_get()
            and "stale Pending Point3D 표시 복원 실패"
            in settings.warning_summary
            and bpy.ops.colmap.review_build_timeline() == {"FINISHED"},
            settings.review_status,
        )
        session = current_review_session(bpy.context, strict=True)

        # Apply 실패는 권위 모델을 rollback하고 Pending을 그대로 보존합니다.
        assert_true(
            bpy.ops.colmap.review_previous() == {"FINISHED"}
            and session.current_image_id == 5
            and bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        original_synchronize_scene = edit_workflows.synchronize_scene
        edit_workflows.synchronize_scene = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("의도한 Pending Apply 실패"))
        )
        try:
            try:
                failed_apply = bpy.ops.colmap.review_apply_pending_filters()
            except RuntimeError:
                failed_apply = {"CANCELLED"}
        finally:
            edit_workflows.synchronize_scene = original_synchronize_scene
        runtime = ensure_runtime(scene)
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            failed_apply == {"CANCELLED"}
            and settings.edit_revision == 0
            and settings.review_pending_camera_count == 1
            and session.pending_image_ids == frozenset({5})
            and set(runtime.current_reconstruction.images) == {2, 5, 9, 12}
            and set(runtime.current_reconstruction.points3d) == {100, 200, 300},
            "Apply 실패가 권위 모델과 Pending 표시를 원자적으로 복구하지 못했습니다.",
        )

        # Apply는 단일 이력 commit 뒤 Timeline을 정확히 한 번 자동 갱신합니다.
        automatic_refresh_calls = []
        original_automatic_refresh = (
            camera_review_timeline.build_or_refresh_timeline
        )

        def count_automatic_refresh(*args, **kwargs):
            automatic_refresh_calls.append((args, kwargs))
            return original_automatic_refresh(*args, **kwargs)

        camera_review_timeline.build_or_refresh_timeline = (
            count_automatic_refresh
        )
        try:
            successful_apply = bpy.ops.colmap.review_apply_pending_filters()
        finally:
            camera_review_timeline.build_or_refresh_timeline = (
                original_automatic_refresh
            )
        assert_true(
            successful_apply == {"FINISHED"}
            and len(automatic_refresh_calls) == 1,
            "Pending Apply가 Timeline을 정확히 한 번 자동 갱신하지 못했습니다.",
        )
        runtime = ensure_runtime(scene)
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            settings.edit_revision == 1
            and settings.review_pending_filter_json == "{}"
            and settings.review_pending_filter_order_json == "[]"
            and set(runtime.current_reconstruction.images) == {2, 9, 12}
            and set(runtime.current_reconstruction.points3d) == {100}
            and runtime.current_reconstruction.images[2].points2d[1].point3d_id is None
            and runtime.current_reconstruction.images[9].points2d[0].point3d_id is None,
            "Apply가 Pending Camera와 관측 Point3D를 단일 편집으로 반영하지 못했습니다.",
        )

        # Apply → Undo는 모델과 Review/Timeline을 복구합니다.
        assert_true(
            bpy.ops.colmap.undo_last_edit() == {"FINISHED"},
            settings.point_selection_status,
        )
        runtime = ensure_runtime(scene)
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            settings.edit_revision == 0
            and set(runtime.current_reconstruction.images) == {2, 5, 9, 12}
            and set(runtime.current_reconstruction.points3d) == {100, 200, 300}
            and session.timeline_active,
            "Apply → Undo가 Runtime 또는 Review/Timeline을 복구하지 못했습니다.",
        )

        # 마지막 Camera 등록은 새 마지막으로 Clamp하고 Apply·Reset을 지원합니다.
        while settings.review_current_index < settings.review_total_count - 1:
            assert_true(
                bpy.ops.colmap.review_next() == {"FINISHED"},
                settings.review_status,
            )
        assert_true(
            bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            session.current_image_id == 9,
            "마지막 Camera Pending 등록 뒤 새 마지막 Camera로 Clamp하지 못했습니다.",
        )
        assert_true(
            bpy.ops.colmap.review_apply_pending_filters() == {"FINISHED"}
            and bpy.ops.colmap.reset_edits() == {"FINISHED"},
            settings.point_selection_status,
        )
        runtime = ensure_runtime(scene)
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            settings.edit_revision == 0
            and set(runtime.current_reconstruction.images) == {2, 5, 9, 12}
            and set(runtime.current_reconstruction.points3d) == {100, 200, 300}
            and session.current_image_id == 9
            and session.timeline_active,
            "마지막 Pending Apply → Reset이 Runtime 또는 Review/Timeline을 복구하지 못했습니다.",
        )

        # 활성 Review Import commit 실패는 기존 논리 세션과 Look Through를 복구합니다.
        scene.camera = scene_camera_before_import
        failed_transition_context = _ViewportContext(scene)
        failed_transition_original = (
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.pixel_aspect_x,
            scene.render.pixel_aspect_y,
            scene.camera,
        )
        look_through_current(failed_transition_context, session)
        active_transition_render = (
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.pixel_aspect_x,
            scene.render.pixel_aspect_y,
            scene.camera,
        )
        session_before_import_failure = session
        original_import_commit = operators_import.commit_staging
        operators_import.commit_staging = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("의도한 Import commit 실패"))
        )
        settings.dataset_root = str(root)
        try:
            try:
                failed_import = bpy.ops.colmap.import_model()
            except RuntimeError:
                failed_import = {"CANCELLED"}
        finally:
            operators_import.commit_staging = original_import_commit
        assert_true(
            failed_import == {"CANCELLED"}
            and current_review_session(bpy.context) is session_before_import_failure
            and failed_transition_context.space_data.region_3d.view_perspective
            == "CAMERA"
            and settings.review_restore_available
            and (
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
                scene.render.pixel_aspect_x,
                scene.render.pixel_aspect_y,
                scene.camera,
            )
            == active_transition_render,
            "Import commit 실패가 기존 Review 논리 세션 또는 Look Through를 복구하지 못했습니다.",
        )

        # 같은 Dataset reload도 commit 전에 Review를 복원하고 새 세션을 초기화합니다.
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        assert_true(
            current_review_session(bpy.context) is not None,
            f"같은 Dataset reload 뒤 Review Session이 없습니다: {settings.review_status}",
        )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            failed_transition_context.space_data.region_3d.view_perspective
            == "PERSP"
            and (
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
                scene.render.pixel_aspect_x,
                scene.render.pixel_aspect_y,
            )
            == failed_transition_original[:-1]
            and scene.camera is session.camera_objects[2]
            and (session.current_index, session.current_image_id) == (0, 2),
            "같은 Dataset reload가 View/Render를 복원하고 자동 Timeline Camera를 연결하지 못했습니다.",
        )

        # 다른 Dataset Import는 구 관리 Camera 제거 전에 자유 시점과 Render를 복원합니다.
        dataset_a_uuid = session.dataset_uuid
        scene.camera = scene_camera_before_import
        dataset_b_context = _ViewportContext(scene)
        dataset_b_original = (
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.pixel_aspect_x,
            scene.render.pixel_aspect_y,
            scene.camera,
        )
        look_through_current(dataset_b_context, session)
        dataset_b_root = root / "dataset_b"
        _make_review_fixture(dataset_b_root)
        settings.dataset_root = str(dataset_b_root)
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        assert_true(
            current_review_session(bpy.context) is not None,
            f"A→B Import 뒤 Review Session이 없습니다: {settings.review_status}",
        )
        session = current_review_session(bpy.context, strict=True)
        assert_true(
            session.dataset_uuid != dataset_a_uuid
            and dataset_b_context.space_data.region_3d.view_perspective
            == "PERSP"
            and (
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
                scene.render.pixel_aspect_x,
                scene.render.pixel_aspect_y,
            )
            == dataset_b_original[:-1]
            and scene.camera is session.camera_objects[2],
            "다른 Dataset Import가 View/Render를 복원하고 새 Timeline Camera를 연결하지 못했습니다.",
        )

        # 연속 등록 뒤 한 번의 Apply로 빈 Review와 Viewport snapshot을 정리합니다.
        while settings.review_total_count > 1:
            assert_true(
                bpy.ops.colmap.review_filter_current() == {"FINISHED"},
                settings.review_status,
            )
        session = current_review_session(bpy.context, strict=True)
        unique_image_id = session.current_image_id
        empty_view_context = _ViewportContext(scene)
        look_through_current(empty_view_context, session)
        assert_true(
            bpy.ops.colmap.review_filter_current() == {"FINISHED"},
            settings.review_status,
        )
        assert_true(
            settings.edit_revision == 0
            and settings.review_pending_camera_count == 4
            and bpy.ops.colmap.review_apply_pending_filters() == {"FINISHED"},
            settings.review_status,
        )
        runtime = ensure_runtime(scene)
        session = current_review_session(bpy.context, strict=True)
        empty_manifest = json.loads(settings.review_marker_manifest_json)
        assert_true(
            unique_image_id is not None
            and not runtime.current_reconstruction.images
            and (session.current_index, session.current_image_id) == (-1, None)
            and not session.timeline_active
            and not session.viewport_snapshots
            and session.render_snapshot is None
            and not any(
                item["dataset_uuid"] == session.dataset_uuid
                for item in empty_manifest
            ),
            "전체 Pending Apply 뒤 빈 Review/View/Timeline을 안전하게 정리하지 못했습니다.",
        )
        assert_true(
            bpy.ops.colmap.reset_edits() == {"FINISHED"},
            settings.point_selection_status,
        )

        # 실제 multi-sensor Rig의 부분 Camera Filter는 변경 전에 거부합니다.
        modern_root = root / "modern_filter_dataset"
        make_modern_text(modern_root)
        settings.dataset_root = str(modern_root)
        settings.input_format = "TEXT"
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        modern_runtime = ensure_runtime(scene)
        modern_session = ensure_review_session(bpy.context)
        try:
            rig_filter = bpy.ops.colmap.review_filter_current()
        except RuntimeError:
            rig_filter = {"CANCELLED"}
        assert_true(
            rig_filter == {"CANCELLED"}
            and settings.edit_revision == 0
            and ensure_runtime(scene) is modern_runtime
            and current_review_session(bpy.context) is modern_session,
            "multi-sensor Rig Filter 거부가 Runtime/Review를 변경했습니다.",
        )
        state_handlers._PENDING_RESTORE_SCHEDULED = True
        bpy.app.timers.register(
            state_handlers._restore_pending_after_state_change,
            first_interval=3600.0,
        )

        # ReviewSession이 유실된 Extension 해제도 모든 Scene의 정확 소유 Marker만 정리합니다.
        unregister_manifest = [
            entry
            for entry in json.loads(settings.review_marker_manifest_json)
            if entry["dataset_uuid"] == modern_session.dataset_uuid
        ]
        settings.review_marker_manifest_json = json.dumps(
            unregister_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        unregister_owned_names = tuple(
            entry["marker_name"] for entry in unregister_manifest
        )
        unregister_camera_pointers = tuple(
            camera.as_pointer() for camera in modern_session.camera_objects.values()
        )
        unregister_user_marker = scene.timeline_markers.new(
            "Extension 해제 보존 사용자 Marker",
            frame=-500,
        )

        second_scene = bpy.data.scenes.new("Modifier Unregister Second Scene")
        second_settings = second_scene.colmap_import
        second_settings.dataset_uuid = "modifier-unregister-second"
        second_camera_data = bpy.data.cameras.new("두 번째 Scene Camera Data")
        second_camera = bpy.data.objects.new("두 번째 Scene Camera", second_camera_data)
        second_scene.collection.objects.link(second_camera)
        second_camera[OWNER_KEY] = OWNER_TOKEN
        second_camera[RECORD_KIND_KEY] = "CAMERA"
        second_camera[DATASET_UUID_KEY] = second_settings.dataset_uuid
        second_camera["colmap_image_id_text"] = "41"
        second_marker = second_scene.timeline_markers.new(
            camera_review_timeline.marker_base_name(second_settings.dataset_uuid, 41),
            frame=1,
        )
        second_marker.camera = second_camera
        second_marker_name = second_marker.name
        second_settings.review_marker_manifest_json = json.dumps(
            [
                {
                    "dataset_uuid": second_settings.dataset_uuid,
                    "source_fingerprint": "second-source",
                    "edit_revision": 0,
                    "marker_name": second_marker.name,
                    "frame": second_marker.frame,
                    "image_id": "41",
                    "camera": {
                        "dataset_uuid": second_settings.dataset_uuid,
                        "image_id": "41",
                        "name": second_camera.name,
                    },
                }
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        second_user_marker = second_scene.timeline_markers.new(
            "두 번째 Scene 사용자 Marker",
            frame=-1,
        )
        camera_review_timeline.acquire_modifier_marker_session(second_scene)
        camera_review_session.clear_review_sessions(bpy.context)
        assert_true(
            current_review_session(bpy.context) is None
            and all(scene.timeline_markers.get(name) is not None for name in unregister_owned_names),
            "ReviewSession 유실 기준 Marker를 준비하지 못했습니다.",
        )
        modifier_workspace._deactivate_modifier_marker_session(bpy.context)
        assert_true(
            all(scene.timeline_markers.get(name) is None for name in unregister_owned_names)
            and settings.review_marker_manifest_json
            and not camera_review_timeline.modifier_marker_session_owned(scene),
            "ReviewSession 없는 Workspace 종료가 Scene-only Marker/lease를 정리하지 못했습니다.",
        )
        camera_review_timeline.acquire_modifier_marker_session(
            scene,
            window=bpy.context.window,
        )
        modern_session = ensure_review_session(bpy.context)
        camera_review_timeline.attach_review_timeline_markers(modern_session)
        camera_review_session.clear_review_sessions(bpy.context)

        second_marker.frame = 2
        try:
            lifecycle._detach_modifier_markers_before_unregister()
            ambiguous_unregister_blocked = False
        except ValueError:
            ambiguous_unregister_blocked = True
        assert_true(
            ambiguous_unregister_blocked
            and second_scene.timeline_markers.get(second_marker_name) is not None
            and camera_review_timeline.modifier_marker_session_owned(scene)
            and camera_review_timeline.modifier_marker_session_owned(second_scene),
            "모호한 Marker를 보존하거나 Extension 해제와 lease 반납을 차단하지 못했습니다.",
        )
        second_marker.frame = 1
        blender_colmap.unregister()
        assert_true(
            all(scene.timeline_markers.get(name) is None for name in unregister_owned_names)
            and second_scene.timeline_markers.get(second_marker_name) is None
            and scene.timeline_markers.get(unregister_user_marker.name) is not None
            and second_scene.timeline_markers.get(second_user_marker.name) is not None
            and tuple(camera.as_pointer() for camera in modern_session.camera_objects.values())
            == unregister_camera_pointers
            and second_camera.as_pointer() != 0
            and not camera_review_timeline.modifier_marker_session_owned(scene)
            and not camera_review_timeline.modifier_marker_session_owned(second_scene),
            "Session 없는 Extension 해제가 모든 Scene의 정확 소유 Marker/lease만 정리하지 못했습니다.",
        )
finally:
    try:
        session = current_review_session(bpy.context)
        if session is not None:
            camera_review_timeline.detach_review_timeline_markers(session)
        camera_review_timeline.release_modifier_marker_session(bpy.context.scene)
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        pass
    blender_colmap.unregister()

assert_true(
    not hasattr(bpy.types.Scene, "colmap_import"),
    "Extension 해제 뒤 Scene Property가 남았습니다.",
)
assert_true(
    camera_review_timeline.frame_change_post
    not in bpy.app.handlers.frame_change_post,
    "Extension 해제 뒤 Camera Review frame handler가 남았습니다.",
)
assert_true(
    camera_review_timeline.timeline_marker_guard_post
    not in bpy.app.handlers.depsgraph_update_post,
    "Extension 해제 뒤 Camera Review Marker 보호 handler가 남았습니다.",
)
assert_true(
    not bpy.app.timers.is_registered(
        camera_review_timeline.process_pending_timeline_updates
    ),
    "Extension 해제 뒤 Camera Review timer가 남았습니다.",
)
assert_true(
    not bpy.app.timers.is_registered(
        camera_review_timeline.process_pending_marker_guard_updates
    ),
    "Extension 해제 뒤 Camera Review Marker 보호 timer가 남았습니다.",
)
assert_true(
    not bpy.app.timers.is_registered(
        state_handlers._restore_pending_after_state_change
    ),
    "Extension 해제 뒤 Pending 복원 timer가 남았습니다.",
)
assert_true(
    not camera_review_keymap._KEYMAP_ITEMS,
    "Extension 해제 뒤 Modifier Camera Review F7 키맵이 남았습니다.",
)
print("BLENDER_COLMAP_BACKGROUND_CAMERA_REVIEW_OK")
