"""Blender 5.2 background에서 Extension 등록과 Import staging을 검증합니다."""

from __future__ import annotations

import sys
import tempfile
import inspect
from dataclasses import replace
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier as blender_colmap
from tools.modifier.core import camera_review as camera_review_core
from tools.modifier.blender import scene_bridge, scene_elements, scene_transaction
from tools.modifier.blender.delete_guard import COLMAP_OT_block_native_delete
from tools.modifier.blender.operators_camera_edit_mode import (
    COLMAP_OT_toggle_edit_camera_mode,
)
from tools.modifier.blender.operators_import import (
    COLMAP_OT_import,
    _enable_point_colors_in_viewport,
)
from tools.modifier.blender.operators_export import COLMAP_OT_export
from tools.modifier.blender.operators_point_edit_mode import (
    COLMAP_OT_toggle_edit_point_mode,
)
from tools.modifier.blender.operators_point_selection import (
    COLMAP_OT_remove,
    COLMAP_OT_reset_edits,
    COLMAP_OT_select_observing_cameras,
    COLMAP_OT_undo_last_edit,
)
from tools.modifier.blender.ownership import (
    DATASET_UUID_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    SOURCE_FINGERPRINT_KEY,
)
from tools.modifier.blender.axis_display import (
    display_axis_matrix,
    migrate_legacy_axis_preset,
)
from tools.modifier.blender.panels import CLASSES as PANEL_CLASSES
from tools.modifier.blender.panels import (
    COLMAP_PT_import,
    COLMAP_PT_modifier,
    COLMAP_PT_status,
)
from tools.modifier.blender.point_display import sync_point_scale_from_modifiers
from tools.modifier.blender.runtime_state import ensure_runtime, loaded_for_current
from tools.modifier.blender.status_display import (
    STATUS_FILLER_TEXT,
    STATUS_VISIBLE_ROWS,
    build_status_display_lines,
    display_cell_width,
    status_cell_limit,
    sync_status_display_lines,
)
from tools.modifier.core.edit_replay import history_hash
from tools.modifier.core.model import Reconstruction
from tests.modifier.fixture_utils import make_legacy_text


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _custom_property_token(data_block):
    return tuple(sorted((str(key), repr(value)) for key, value in data_block.items()))


def _matrix_token(matrix):
    return tuple(round(value, 12) for row in matrix for value in row)


def _managed_scene_signature(scene):
    roots = [
        collection
        for collection in scene.collection.children
        if collection.get(OWNER_KEY) == OWNER_TOKEN
        and collection.get(RECORD_KIND_KEY) == "ROOT"
    ]
    assert_true(len(roots) == 1, "Scene signature의 관리 Root가 정확히 하나가 아닙니다.")
    root = roots[0]
    child_collections = tuple(
        sorted(
            (
                child.get(RECORD_KIND_KEY),
                child.as_pointer(),
                _custom_property_token(child),
            )
            for child in root.children
            if child.get(OWNER_KEY) == OWNER_TOKEN
        )
    )
    point_objects = [
        obj
        for obj in bpy.data.objects
        if obj.get(OWNER_KEY) == OWNER_TOKEN
        and obj.get(RECORD_KIND_KEY) == "POINT_MESH"
        and obj.get(DATASET_UUID_KEY) == root.get(DATASET_UUID_KEY)
    ]
    assert_true(len(point_objects) == 1, "Scene signature의 Point Object가 잘못되었습니다.")
    point = point_objects[0]
    modifier = [
        item
        for item in point.modifiers
        if item.type == "NODES"
        and item.node_group is not None
        and item.node_group.get(RECORD_KIND_KEY) == "POINT_DISPLAY_NODES"
    ][0]
    scale_identifier = modifier.node_group["colmap_point_scale_socket"]
    point_token = (
        point.as_pointer(),
        point.data.as_pointer(),
        modifier.node_group.as_pointer(),
        getattr(modifier.properties.inputs, scale_identifier).value,
        point.parent.as_pointer() if point.parent else 0,
        _matrix_token(point.matrix_parent_inverse),
        _matrix_token(point.matrix_basis),
        _custom_property_token(point),
        point.hide_select,
        point.select_get(),
    )
    camera_token = tuple(
        sorted(
            (
                obj["colmap_image_id_text"],
                obj.as_pointer(),
                obj.data.as_pointer(),
                obj.parent.as_pointer() if obj.parent else 0,
                _matrix_token(obj.matrix_parent_inverse),
                _matrix_token(obj.matrix_basis),
                _custom_property_token(obj),
                obj.hide_select,
                obj.select_get(),
                tuple(sorted(collection.name for collection in obj.users_collection)),
            )
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN
            and obj.get(RECORD_KIND_KEY) == "CAMERA"
            and obj.get(DATASET_UUID_KEY) == root.get(DATASET_UUID_KEY)
        )
    )
    axis_token = tuple(
        sorted(
            (
                obj.as_pointer(),
                _matrix_token(obj.matrix_world),
                _custom_property_token(obj),
            )
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN
            and obj.get(RECORD_KIND_KEY) == "AXIS_ROOT"
            and obj.get(DATASET_UUID_KEY) == root.get(DATASET_UUID_KEY)
        )
    )
    return (
        root.as_pointer(),
        _custom_property_token(root),
        child_collections,
        point_token,
        camera_token,
        axis_token,
        tuple(
            view_layer.objects.active.as_pointer()
            if view_layer.objects.active is not None
            else 0
            for view_layer in scene.view_layers
        ),
    )


def _expect_transaction_rollback(scene, loaded, stage_name, label):
    before = _managed_scene_signature(scene)
    staged = scene_bridge.build_staging(loaded, 0.123, "X", "Z")
    original_stage = getattr(scene_transaction, stage_name)

    def fail_after_stage(transaction):
        original_stage(transaction)
        raise RuntimeError(f"의도한 {label} 실패")

    setattr(scene_transaction, stage_name, fail_after_stage)
    try:
        try:
            scene_bridge.commit_staging(scene, staged)
        except RuntimeError as exc:
            assert_true(
                f"의도한 {label} 실패" in str(exc),
                f"{label} 실패 주입과 다른 예외가 발생했습니다: {exc}",
            )
        else:
            raise AssertionError(f"{label} 실패 주입이 commit을 중단하지 않았습니다.")
    finally:
        setattr(scene_transaction, stage_name, original_stage)
        scene_bridge.cleanup_staging(staged)
    assert_true(
        _managed_scene_signature(scene) == before,
        f"{label} 실패 뒤 관리 Scene signature가 완전히 복구되지 않았습니다.",
    )


class _ViewportArea:
    type = "VIEW_3D"

    def __init__(self):
        self.redrawn = False

    def tag_redraw(self):
        self.redrawn = True


class _ViewportContext:
    def __init__(self):
        self.area = _ViewportArea()
        self.space_data = type(
            "Space",
            (),
            {
                "shading": type("Shading", (), {"color_type": "MATERIAL"})(),
                "overlay": type(
                    "Overlay",
                    (),
                    {"show_relationship_lines": True},
                )(),
            },
        )()


class _StatusLayout:
    def __init__(self):
        self.labels = []
        self.template_lists = []

    def box(self):
        return self

    def label(self, *, text="", **_kwargs):
        self.labels.append(text)

    def template_list(
        self,
        listtype_name,
        list_id,
        dataptr,
        propname,
        active_dataptr,
        active_propname,
        **kwargs,
    ):
        del active_dataptr, active_propname
        self.template_lists.append((listtype_name, list_id, kwargs))
        self.labels.extend(item.text for item in getattr(dataptr, propname))


class _PanelLayout:
    """실제 Region 없이 모든 패널 draw의 Python 경계를 실행합니다."""

    def __init__(self):
        self.enabled = True

    def box(self):
        return _PanelLayout()

    def row(self, **_kwargs):
        return _PanelLayout()

    def column(self, **_kwargs):
        return _PanelLayout()

    def label(self, **_kwargs):
        return None

    def separator(self):
        """패널 구분선 그리기를 허용합니다."""

        return None

    def prop(self, *_args, **_kwargs):
        return None

    def operator(self, operator_id, **_kwargs):
        del operator_id
        return None

    def template_list(self, *_args, **_kwargs):
        return None


class _AxisPanelLayout:
    """Display Axis 조건부 UI가 그린 속성과 문구를 기록합니다."""

    def __init__(self, trace=None):
        """중첩 Layout이 공유할 UI 호출 기록을 준비합니다."""

        self.trace = trace if trace is not None else {
            "properties": [],
            "labels": [],
        }

    def box(self):
        """같은 기록을 공유하는 Box Layout을 반환합니다."""

        return _AxisPanelLayout(self.trace)

    def row(self, **_kwargs):
        """같은 기록을 공유하는 Row Layout을 반환합니다."""

        return _AxisPanelLayout(self.trace)

    def label(self, *, text="", **_kwargs):
        """패널이 표시한 Label 문구를 기록합니다."""

        self.trace["labels"].append(text)

    def separator(self):
        """같은 Box 안의 Point·Camera 구분선 호출을 허용합니다."""

        return None

    def prop(self, _data, property_name, **_kwargs):
        """패널이 표시한 RNA 속성 이름을 기록합니다."""

        self.trace["properties"].append(property_name)

    def operator(self, _operator_id, **_kwargs):
        """Import 버튼 그리기를 허용하는 빈 연산자 결과를 반환합니다."""

        return None


blender_colmap.register()
try:
    operator_contract = {
        COLMAP_OT_import: (
            "colmap.import_model",
            "Import",
            {"REGISTER", "UNDO", "BLOCKING"},
        ),
        COLMAP_OT_export: ("colmap.export", "Export", {"REGISTER"}),
        COLMAP_OT_toggle_edit_point_mode: (
            "colmap.toggle_edit_point_mode",
            "Edit Point Mode",
            {"REGISTER"},
        ),
        COLMAP_OT_toggle_edit_camera_mode: (
            "colmap.toggle_edit_camera_mode",
            "Edit Camera Mode",
            {"REGISTER"},
        ),
        COLMAP_OT_select_observing_cameras: (
            "colmap.select_observing_cameras",
            "Select Observing Cameras",
            {"REGISTER"},
        ),
        COLMAP_OT_remove: (
            "colmap.remove",
            "Remove",
            {"REGISTER", "UNDO"},
        ),
        COLMAP_OT_undo_last_edit: (
            "colmap.undo_last_edit",
            "Undo Last Edit",
            {"REGISTER", "UNDO"},
        ),
        COLMAP_OT_reset_edits: (
            "colmap.reset_edits",
            "Reset Edits",
            {"REGISTER", "UNDO"},
        ),
        COLMAP_OT_block_native_delete: (
            "colmap.block_native_delete",
            "COLMAP Delete 차단",
            {"INTERNAL"},
        ),
    }
    for operator, expected in operator_contract.items():
        assert_true(
            (
                operator.bl_idname,
                operator.bl_label,
                set(operator.bl_options),
            )
            == expected,
            f"{operator.__name__}의 ID, label 또는 options 계약이 바뀌었습니다.",
        )
    assert_true(
        scene_bridge.StagedScene is scene_transaction.StagedScene
        and scene_bridge.build_staging is scene_transaction.build_staging
        and scene_bridge.commit_staging is scene_transaction.commit_staging
        and scene_bridge.cleanup_staging is scene_transaction.cleanup_staging
        and scene_bridge.point_scale_socket_identifier
        is scene_elements.point_scale_socket_identifier,
        "scene_bridge 호환 파사드가 분리된 Scene 구현을 그대로 노출하지 않습니다.",
    )
    assert_true(
        PANEL_CLASSES[0].bl_idname == "COLMAP_PT_modifier",
        "COLMAP Modifier 부모 패널이 첫 번째로 등록되지 않았습니다.",
    )
    assert_true(
        tuple((panel.bl_idname, panel.bl_category) for panel in PANEL_CLASSES)
        == (
            ("COLMAP_PT_modifier", "COLMAP Modifier"),
            ("COLMAP_PT_status", "COLMAP Modifier"),
            ("COLMAP_PT_import", "COLMAP Modifier"),
            ("COLMAP_PT_point_selection", "COLMAP Modifier"),
            ("COLMAP_PT_camera_review", "COLMAP Modifier"),
            ("COLMAP_PT_export", "COLMAP Modifier"),
        ),
        "Panel ID 또는 Sidebar category 계약이 바뀌었습니다.",
    )
    assert_true(
        not hasattr(bpy.types, "COLMAP_PT_projection_paint")
        and bpy.types.Operator.bl_rna_get_subclass_py(
            "COLMAP_OT_toggle_projection_paint"
        )
        is None
        and bpy.types.Operator.bl_rna_get_subclass_py(
            "COLMAP_OT_projection_layer_add"
        )
        is None,
        "분리 대상인 Projection Paint UI 또는 연산자가 등록되었습니다.",
    )
    assert_true(
        bpy.app.handlers.depsgraph_update_post.count(sync_point_scale_from_modifiers) == 1,
        "Point Scale 역동기화 핸들러가 없거나 중복 등록되었습니다.",
    )
    viewport_context = _ViewportContext()
    assert_true(
        _enable_point_colors_in_viewport(viewport_context),
        "Solid Viewport Color Attribute 설정이 실패했습니다.",
    )
    assert_true(
        viewport_context.space_data.shading.color_type == "VERTEX"
        and not viewport_context.space_data.overlay.show_relationship_lines
        and viewport_context.area.redrawn,
        "Import Viewport의 Color Attribute 또는 Relation Line 설정이 잘못되었습니다.",
    )
    status_layout = _StatusLayout()
    status_panel = type("StatusPanel", (), {"layout": status_layout})()
    COLMAP_PT_status.draw(status_panel, bpy.context)
    assert_true(
        f"Version: {blender_colmap.__version__}" in status_layout.labels,
        "첫 실행 Status에 현재 애드온 버전이 표시되지 않았습니다.",
    )
    assert_true(
        status_layout.template_lists
        == [
            (
                "COLMAP_UL_status_lines",
                "status_display_5_rows",
                {
                    "rows": STATUS_VISIBLE_ROWS,
                    "maxrows": STATUS_VISIBLE_ROWS,
                    "sort_lock": True,
                },
            )
        ],
        "Status UIList가 고정 행 수로 그려지지 않았습니다.",
    )
    assert_true(
        len(bpy.context.scene.colmap_import.status_display_lines)
        == STATUS_VISIBLE_ROWS
        and bpy.context.scene.colmap_import.status_display_lines[-1].text
        == STATUS_FILLER_TEXT,
        "첫 Panel draw 전에 비어 보이는 5행 Status 표시 데이터가 준비되지 않았습니다.",
    )
    settings = bpy.context.scene.colmap_import
    removed_projection_properties = {
        "projection_paint_target",
        "projection_paint_mode_active",
        "projection_layer_document_uuid",
        "projection_layer_schema_version",
        "projection_layer_background_rgb",
        "projection_layer_target_uuid",
        "projection_layer_material",
        "projection_layers",
        "projection_active_layer_index",
        "projection_active_paint_target",
        "projection_layer_document_revision",
        "projection_flattened_image",
        "projection_flattened_revision",
        "projection_layer_job_running",
        "projection_layer_stroke_monitor_token",
        "projection_mask_preview_item_uuid",
        "projection_mask_preview_status",
    }
    assert_true(
        not removed_projection_properties
        & {prop.identifier for prop in settings.bl_rna.properties},
        "분리 대상인 Projection Paint Scene 속성이 등록되었습니다.",
    )
    settings.status_display_index = 0
    assert_true(
        settings.status_display_index == -1,
        "읽기 전용 Status UIList에 파란색 선택 행이 생길 수 있습니다.",
    )
    assert_true(
        hasattr(bpy.types, "COLMAP_UL_status_lines"),
        "Status UIList가 등록되지 않았습니다.",
    )
    assert_true(
        "DEFAULT_CLOSED" not in getattr(COLMAP_PT_modifier, "bl_options", set())
        and "DEFAULT_CLOSED" not in getattr(COLMAP_PT_status, "bl_options", set())
        and all(
            "DEFAULT_CLOSED" in getattr(panel_class, "bl_options", set())
            for panel_class in PANEL_CLASSES
            if panel_class not in {COLMAP_PT_modifier, COLMAP_PT_status}
        ),
        "부모와 Status를 제외한 Modifier 패널이 기본 접힘 상태가 아닙니다.",
    )

    original_status = settings.status
    original_status_kind = settings.status_kind
    original_point_selection_status = settings.point_selection_status
    original_review_status = settings.review_status
    original_export_status = settings.export_status
    original_warning_summary = settings.warning_summary
    original_latest_status_message = settings.latest_status_message
    original_latest_status_icon = settings.latest_status_icon
    settings.status = "Import 상태"
    assert_true(
        settings.latest_status_message == "Import: Import 상태",
        "Import 상태가 최신 Status 메시지로 기록되지 않았습니다.",
    )
    settings.point_selection_status = "Point 편집 상태"
    assert_true(
        settings.latest_status_message == "Edit: Point 편집 상태",
        "Point 편집 상태가 최신 Status 메시지로 교체되지 않았습니다.",
    )
    settings.review_status = "Camera Review 상태"
    assert_true(
        settings.latest_status_message == "Review: Camera Review 상태",
        "Camera Review 상태가 최신 Status 메시지로 교체되지 않았습니다.",
    )
    settings.export_status = "Export 상태"
    assert_true(
        settings.latest_status_message == "Export: Export 상태",
        "Export 상태가 최신 Status 메시지로 교체되지 않았습니다.",
    )
    settings.warning_summary = "이전 경고\n가장 최근 경고"
    assert_true(
        settings.latest_status_message == "Warning: 가장 최근 경고"
        and settings.latest_status_icon == "ERROR",
        "Warning의 마지막 행이 최신 Status 메시지로 기록되지 않았습니다.",
    )
    settings.status_kind = "ERROR"
    settings.status = (
        "아주 긴 COLMAP 임포트 상태 문장이 좁은 Sidebar에서도 잘리지 않고 "
        "여러 줄로 표시되어야 합니다. 가장 최근 상태 메시지 하나만 표시하더라도 "
        "고정 높이 다섯 행을 넘는 문장은 Blender 기본 UIList 스크롤로 끝까지 "
        "확인할 수 있어야 하며 이전 Import, Edit, Review와 Warning 메시지는 "
        "동시에 남아 있지 않아야 합니다."
    )
    wide_lines = build_status_display_lines(settings, 440, 1.0)
    narrow_lines = build_status_display_lines(settings, 190, 1.0)
    narrow_limit = status_cell_limit(190, 1.0)
    assert_true(
        len(narrow_lines) > len(wide_lines)
        and all(
            display_cell_width(text) <= narrow_limit
            for text, _icon in narrow_lines
        )
        and narrow_lines[0][1] == "ERROR"
        and all(
            not text.startswith(("Edit:", "Review:", "Warning:"))
            for text, _icon in narrow_lines
        ),
        "Status 문장이 Sidebar 폭 변경에 맞춰 안전하게 다시 줄바꿈되지 않았습니다.",
    )
    sync_status_display_lines(settings, 190, 1.0)
    assert_true(
        tuple(
            (item.text, item.icon)
            for item in settings.status_display_lines
        )
        == narrow_lines
        and len(narrow_lines) > STATUS_VISIBLE_ROWS,
        "고정 높이 Status UIList에 스크롤할 표시 행이 구성되지 않았습니다.",
    )
    settings.status = original_status
    settings.status_kind = original_status_kind
    settings.point_selection_status = original_point_selection_status
    settings.review_status = original_review_status
    settings.export_status = original_export_status
    settings.warning_summary = original_warning_summary
    settings.latest_status_message = original_latest_status_message
    settings.latest_status_icon = original_latest_status_icon
    sync_status_display_lines(settings, 320, 1.0)

    for panel_class in PANEL_CLASSES:
        panel = type(
            "PanelInstance",
            (),
            {"layout": _PanelLayout()},
        )()
        panel_class.draw(panel, bpy.context)

    scene = bpy.context.scene
    property_contract = {
        "dataset_root": ("STRING", ""),
        "sparse_directory": ("STRING", ""),
        "images_directory": ("STRING", ""),
        "input_format": ("ENUM", "AUTO"),
        "point_size": ("FLOAT", 0.05),
        "camera_frustum_size": ("FLOAT", 1.0),
        "axis_preset": ("ENUM", "REALITY_CAPTURE"),
        "axis_forward": ("ENUM", "Z"),
        "axis_up": ("ENUM", "-Y"),
        "status": ("STRING", "Ready"),
        "status_kind": ("ENUM", "READY"),
        "latest_status_message": ("STRING", ""),
        "latest_status_icon": ("STRING", "NONE"),
        "active_dataset_root": ("STRING", ""),
        "active_sparse_root": ("STRING", ""),
        "active_model_directory": ("STRING", ""),
        "active_images_directory": ("STRING", ""),
        "active_input_format": ("STRING", ""),
        "active_layout": ("STRING", ""),
        "source_fingerprint": ("STRING", ""),
        "dataset_uuid": ("STRING", ""),
        "schema_version": ("STRING", ""),
        "selected_files_json": ("STRING", ""),
        "camera_count": ("STRING", ""),
        "image_count": ("STRING", ""),
        "point3d_count": ("STRING", ""),
        "warning_summary": ("STRING", ""),
        "edit_history_json": ("STRING", "[]"),
        "edit_revision": ("INT", 0),
        "history_hash": ("STRING", history_hash(())),
        "last_operation_id": ("STRING", ""),
        "selected_point_count": ("INT", 0),
        "observing_camera_count": ("INT", 0),
        "derived_orphan_count": ("INT", 0),
        "point_edit_mode_active": ("BOOLEAN", False),
        "camera_edit_mode_active": ("BOOLEAN", False),
        "camera_target_count": ("INT", 0),
        "remove_target": ("ENUM", "NONE"),
        "point_selection_status": ("STRING", "Point3D를 선택하십시오."),
        "export_backup_enabled": ("BOOLEAN", True),
        "export_output_format": ("ENUM", "SOURCE"),
        "export_running": ("BOOLEAN", False),
        "export_status": ("STRING", "Export 준비"),
    }
    for name, (property_type, default) in property_contract.items():
        rna_property = settings.bl_rna.properties[name]
        actual_default = getattr(settings, name)
        default_matches = (
            abs(actual_default - default) < 1.0e-8
            if isinstance(default, float)
            else actual_default == default
        )
        assert_true(
            rna_property.type == property_type and default_matches,
            f"Property {name}의 타입 또는 기본값 계약이 바뀌었습니다.",
        )

    preset_layout = _AxisPanelLayout()
    preset_panel = type("ImportPanel", (), {"layout": preset_layout})()
    COLMAP_PT_import.draw(preset_panel, bpy.context)
    assert_true(
        "axis_preset" in preset_layout.trace["properties"]
        and "axis_forward" not in preset_layout.trace["properties"]
        and "axis_up" not in preset_layout.trace["properties"]
        and "Forward: Z / Up: -Y" in preset_layout.trace["labels"],
        "일반 Display Axis 프리셋 UI가 축 요약만 표시하지 않습니다.",
    )
    assert_true(
        "camera_frustum_size" in preset_layout.trace["properties"],
        "COLMAP Import 패널에 Camera Frustum Size가 표시되지 않습니다.",
    )
    modifier_import_draw_source = inspect.getsource(COLMAP_PT_import.draw)
    assert_true(
        'display_box.prop(settings, "camera_frustum_size")' in modifier_import_draw_source
        and "camera_display_box" not in modifier_import_draw_source
        and "display_box.separator()" not in modifier_import_draw_source,
        "Point와 Camera 표시 설정이 같은 UI Box를 사용하지 않습니다.",
    )
    settings.axis_preset = "CUSTOM"
    custom_layout = _AxisPanelLayout()
    custom_panel = type("ImportPanel", (), {"layout": custom_layout})()
    COLMAP_PT_import.draw(custom_panel, bpy.context)
    assert_true(
        {"axis_preset", "axis_forward", "axis_up"}
        <= set(custom_layout.trace["properties"]),
        "Custom Display Axis UI에 개별 축 필드가 표시되지 않습니다.",
    )

    settings.axis_forward = "Y"
    settings.axis_up = "Z"
    if "axis_preset" in settings.keys():
        del settings["axis_preset"]
    assert_true(
        migrate_legacy_axis_preset(settings)
        and settings.axis_preset == "BLENDER"
        and (settings.axis_forward, settings.axis_up) == ("Y", "Z"),
        "구형 Blender 축 설정을 방향 변경 없이 이전하지 못했습니다.",
    )
    settings.axis_preset = "REALITY_CAPTURE"

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        model, images = make_legacy_text(root)
        settings.input_format = "TEXT"
        settings.point_size = 0.02
        settings.camera_frustum_size = 0.4
        settings.dataset_root = str(root)
        assert_true(
            settings.sparse_directory == str(model.resolve()),
            "Dataset Root 입력 시 sparse/0이 자동 반영되지 않았습니다.",
        )
        assert_true(
            settings.images_directory == str(images.resolve()),
            "Dataset Root 입력 시 images가 자동 반영되지 않았습니다.",
        )

        # UI 대기 경로가 비어 있어도 Import가 Dataset Root에서 다시 탐색해야 합니다.
        settings.sparse_directory = ""
        settings.images_directory = ""
        original_resolver = camera_review_core.resolve_image_path

        def fail_if_review_resolves_image_path(*_args, **_kwargs):
            raise AssertionError("Loader에서 검증한 이미지 경로를 Camera Review가 다시 해석했습니다.")

        camera_review_core.resolve_image_path = fail_if_review_resolves_image_path
        try:
            result = bpy.ops.colmap.import_model()
        finally:
            camera_review_core.resolve_image_path = original_resolver
        assert_true(result == {"FINISHED"}, f"Import가 실패했습니다: {result}, {settings.status}")
        assert_true(
            settings.review_total_count == 1,
            "Camera Review가 Loader 이미지 경로 manifest를 재사용하지 않았습니다.",
        )
        assert_true(
            settings.sparse_directory == str(model.resolve()),
            "Import가 Dataset Root에서 sparse/0을 다시 탐색하지 않았습니다.",
        )
        assert_true(
            settings.images_directory == str(images.resolve()),
            "Import가 Dataset Root에서 images를 다시 탐색하지 않았습니다.",
        )

        owned_roots = [
            collection
            for collection in scene.collection.children
            if collection.get(OWNER_KEY) == OWNER_TOKEN
            and collection.get(RECORD_KIND_KEY) == "ROOT"
        ]
        assert_true(len(owned_roots) == 1, "관리 Root Collection은 정확히 하나여야 합니다.")
        first_root = owned_roots[0]
        first_uuid = first_root[DATASET_UUID_KEY]

        cameras = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "CAMERA"
        ]
        points = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "POINT_MESH"
        ]
        axis_roots = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "AXIS_ROOT"
        ]
        assert_true(len(cameras) == 1, "등록 Image마다 Camera Object 하나가 필요합니다.")
        assert_true(len(points) == 1 and len(points[0].data.vertices) == 1, "Point Mesh 수가 원본과 다릅니다.")
        assert_true(len(axis_roots) == 1, "표시 축 Root가 정확히 하나여야 합니다.")
        first_point_object = points[0]
        first_camera_object = cameras[0]
        first_axis_root = axis_roots[0]
        first_point_data = first_point_object.data
        first_camera_data = first_camera_object.data
        assert_true(
            abs(first_camera_data.display_size - 0.4) < 1.0e-8,
            "Import 시 Camera Frustum Size가 Camera Data에 반영되지 않았습니다.",
        )
        assert_true(
            first_camera_object.hide_select and not first_camera_object.select_get(),
            "Import 직후 관리 Camera가 선택 잠금 상태가 아닙니다.",
        )
        assert_true(
            first_point_object.parent is first_axis_root
            and first_camera_object.parent is first_axis_root,
            "Point와 Camera가 표시 축 Root 아래에 있지 않습니다.",
        )
        assert_true(
            all(
                abs(actual - expected) < 1.0e-6
                for actual, expected in zip(
                    _matrix_token(first_axis_root.matrix_world),
                    _matrix_token(display_axis_matrix("Z", "-Y")),
                )
            )
            and first_axis_root["colmap_display_forward_axis"] == "Z"
            and first_axis_root["colmap_display_up_axis"] == "-Y",
            "기본 RC 표시축이 Axis Root에 적용되지 않았습니다.",
        )
        assert_true(cameras[0]["colmap_image_id_text"] == "7", "IMAGE_ID metadata가 다릅니다.")
        assert_true(tuple(round(value, 6) for value in cameras[0].location) == (-1.0, -2.0, -3.0), "COLMAP camera center 변환이 잘못되었습니다.")
        assert_true(abs(cameras[0].data.lens - 32.4) < 1e-4, "PINHOLE fx가 Blender lens에 반영되지 않았습니다.")
        assert_true(cameras[0].data["colmap_fy"] == 880.0, "원본 fy가 보존되지 않았습니다.")
        assert_true("colmap_point3d_id_high" in points[0].data.attributes, "64비트 Point3D ID high mapping이 없습니다.")
        assert_true(
            points[0].data.color_attributes.active_color.name == "colmap_color",
            "COLMAP RGB 속성이 활성 Color Attribute로 지정되지 않았습니다.",
        )
        point_modifiers = [modifier for modifier in points[0].modifiers if modifier.type == "NODES"]
        assert_true(len(point_modifiers) == 1, "Point 표시용 Geometry Nodes Modifier가 없습니다.")
        point_nodes = point_modifiers[0].node_group
        assert_true(
            point_nodes.get(RECORD_KIND_KEY) == "POINT_DISPLAY_NODES",
            "Point 표시 Node Group의 소유 metadata가 없습니다.",
        )
        mesh_to_points = point_nodes.nodes.get("COLMAP_Mesh_To_Points")
        assert_true(mesh_to_points is not None, "Mesh to Points 노드가 없습니다.")
        point_scale_socket = point_nodes.get("colmap_point_scale_socket")
        assert_true(point_scale_socket is not None, "실시간 Point Scale 입력 식별자가 없습니다.")
        assert_true(
            mesh_to_points.inputs["Radius"].is_linked,
            "Point Scale이 Mesh to Points 반지름에 연결되지 않았습니다.",
        )
        assert_true(
            abs(
                getattr(
                    point_modifiers[0].properties.inputs,
                    point_scale_socket,
                ).value
                - settings.point_size
            )
            < 1e-8,
            "초기 Point Scale이 Modifier 입력에 반영되지 않았습니다.",
        )
        settings.point_size = 0.0375
        assert_true(
            abs(
                getattr(
                    point_modifiers[0].properties.inputs,
                    point_scale_socket,
                ).value
                - 0.0375
            )
            < 1e-8,
            "Point Scale 변경이 Modifier 입력에 실시간 반영되지 않았습니다.",
        )
        user_camera_data = bpy.data.cameras.new("UserCameraData")
        user_camera_data.display_size = 2.0
        user_camera_object = bpy.data.objects.new("UserCamera", user_camera_data)
        scene.collection.objects.link(user_camera_object)
        settings.camera_frustum_size = 0.75
        assert_true(
            abs(first_camera_data.display_size - 0.75) < 1.0e-8,
            "Camera Frustum Size 변경이 관리 Camera에 실시간 반영되지 않았습니다.",
        )
        assert_true(
            abs(user_camera_data.display_size - 2.0) < 1.0e-8,
            "Camera Frustum Size 변경이 비관리 Camera까지 수정했습니다.",
        )
        modifier_scale_input = getattr(
            point_modifiers[0].properties.inputs,
            point_scale_socket,
        )
        modifier_scale_input.value = 0.0525
        points[0].update_tag(refresh={"DATA"})
        bpy.context.view_layer.update()
        assert_true(
            abs(settings.point_size - 0.0525) < 1e-8
            and abs(points[0]["colmap_point_size"] - 0.0525) < 1e-8,
            "Modifier Point Scale 직접 변경이 애드온 UI로 역동기화되지 않았습니다.",
        )
        set_material = point_nodes.nodes.get("COLMAP_Set_Point_Material")
        assert_true(set_material is not None, "Point 색상 Material 연결 노드가 없습니다.")
        point_material = set_material.inputs["Material"].default_value
        assert_true(
            point_material.get(RECORD_KIND_KEY) == "POINT_DISPLAY_MATERIAL",
            "Point 표시 Material의 소유 metadata가 없습니다.",
        )
        color_attribute_node = point_material.node_tree.nodes.get("COLMAP_Color_Attribute")
        assert_true(
            color_attribute_node is not None
            and color_attribute_node.attribute_name == "colmap_color",
            "Point Material이 COLMAP RGB 속성을 읽지 않습니다.",
        )

        # Review 단계에서 저장된 pixel aspect를 적용했을 때 principal point와
        # 서로 다른 fx/fy가 COLMAP 픽셀 투영과 일치해야 합니다.
        scene.render.resolution_x = 1000
        scene.render.resolution_y = 800
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = cameras[0].data["colmap_pixel_aspect_y_over_x"]
        point_camera = Vector((0.2, 0.1, 2.0))
        point_world = cameras[0].matrix_world @ Vector(
            (point_camera.x, -point_camera.y, -point_camera.z)
        )
        projected = world_to_camera_view(scene, cameras[0], point_world)
        projected_u = projected.x * scene.render.resolution_x
        projected_v = (1.0 - projected.y) * scene.render.resolution_y
        expected_u = 900.0 * point_camera.x / point_camera.z + 510.0
        expected_v = 880.0 * point_camera.y / point_camera.z + 390.0
        assert_true(
            abs(projected_u - expected_u) < 1e-4,
            f"PINHOLE x 투영이 다릅니다: Blender={projected_u}, COLMAP={expected_u}",
        )
        assert_true(
            abs(projected_v - expected_v) < 1e-4,
            f"PINHOLE y 투영이 다릅니다: Blender={projected_v}, COLMAP={expected_v}",
        )

        # 표시 축은 Root Empty에만 적용되고 원본 Mesh 좌표와 Camera local pose를
        # 변경하지 않아야 합니다.
        original_camera_basis = first_camera_object.matrix_basis.copy()
        original_camera_world = first_camera_object.matrix_world.copy()
        settings.axis_preset = "BLENDER"
        bpy.context.view_layer.update()
        assert_true(
            (settings.axis_forward, settings.axis_up) == ("Y", "Z")
            and first_axis_root.matrix_world == Matrix.Identity(4),
            "Blender 프리셋이 Identity 표시축을 적용하지 않았습니다.",
        )
        settings.axis_preset = "MAYA"
        assert_true(
            (settings.axis_forward, settings.axis_up) == ("-Z", "Y"),
            "Maya 프리셋 축이 올바르게 적용되지 않았습니다.",
        )
        settings.axis_preset = "HOUDINI"
        assert_true(
            (settings.axis_forward, settings.axis_up) == ("-Z", "Y"),
            "Houdini 프리셋 축이 올바르게 적용되지 않았습니다.",
        )
        settings.axis_preset = "CUSTOM"
        settings.axis_forward = "-Z"
        settings.axis_up = "Y"
        bpy.context.view_layer.update()
        assert_true(
            first_axis_root.matrix_world != Matrix.Identity(4),
            "표시 축 변경이 Axis Root에 반영되지 않았습니다.",
        )
        assert_true(
            tuple(first_point_object.data.vertices[0].co) == (1.0, 2.0, 3.0),
            "표시 축 변경이 원본 Point3D 좌표를 수정했습니다.",
        )
        assert_true(
            first_camera_object.matrix_basis == original_camera_basis
            and first_camera_object.matrix_world != original_camera_world,
            "표시 축은 Camera local pose를 보존하고 world 표시만 바꿔야 합니다.",
        )

        # 관리 Collection에 사용자가 연결한 비소유 데이터는 Reload 정리에서 삭제하지 않습니다.
        user_mesh = bpy.data.meshes.new("UserMesh")
        user_object = bpy.data.objects.new("UserObject", user_mesh)
        first_root.objects.link(user_object)
        user_collection = bpy.data.collections.new("UserCollection")
        first_root.children.link(user_collection)
        (model / "points3D.txt").write_text(
            "# points\n4294967301 2 3 4 40 50 60 0.5 7 0\n",
            encoding="utf-8",
        )
        settings.dataset_root = ""
        reloaded = bpy.ops.colmap.import_model()
        assert_true(reloaded == {"FINISHED"}, f"Dataset Root 없는 직접 Reload가 실패했습니다: {settings.status}")
        assert_true(settings.active_dataset_root == str(root.resolve()), "직접 지정 경로의 Dataset Root 유도가 잘못되었습니다.")
        reloaded_roots = [
            collection
            for collection in scene.collection.children
            if collection.get(OWNER_KEY) == OWNER_TOKEN
            and collection.get(RECORD_KIND_KEY) == "ROOT"
        ]
        assert_true(
            len(reloaded_roots) == 1 and reloaded_roots[0] is first_root,
            "같은 Dataset Reload에서 기존 Root Collection이 교체되었습니다.",
        )
        reloaded_points = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "POINT_MESH"
        ]
        reloaded_cameras = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "CAMERA"
        ]
        assert_true(
            len(reloaded_points) == 1 and reloaded_points[0] is first_point_object,
            "같은 Dataset Reload에서 기존 Point Object가 교체되거나 복제되었습니다.",
        )
        assert_true(
            len(reloaded_cameras) == 1 and reloaded_cameras[0] is first_camera_object,
            "같은 Dataset Reload에서 기존 Camera Object가 교체되거나 복제되었습니다.",
        )
        assert_true(
            first_camera_object.hide_select and not first_camera_object.select_get(),
            "같은 Dataset Reload 뒤 관리 Camera 선택 잠금이 복원되지 않았습니다.",
        )
        assert_true(
            first_point_object.data is not first_point_data
            and tuple(first_point_object.data.vertices[0].co) == (2.0, 3.0, 4.0),
            "같은 Dataset Reload가 기존 Point Object의 Mesh 데이터를 갱신하지 않았습니다.",
        )
        assert_true(
            first_camera_object.data is not first_camera_data,
            "같은 Dataset Reload가 기존 Camera Object의 Camera Data를 갱신하지 않았습니다.",
        )
        assert_true(
            abs(first_camera_object.data.display_size - 0.75) < 1.0e-8,
            "같은 Dataset Reload 뒤 Camera Frustum Size가 유지되지 않았습니다.",
        )
        assert_true(
            ".00" not in first_point_object.name and ".00" not in first_camera_object.name,
            "같은 Dataset Reload 뒤 관리 Object 이름에 복사본 접미사가 생겼습니다.",
        )
        assert_true(
            first_root[DATASET_UUID_KEY] == first_uuid,
            "같은 Dataset Reload에서 Dataset UUID가 변경되었습니다.",
        )
        reloaded_axis_roots = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "AXIS_ROOT"
        ]
        assert_true(
            len(reloaded_axis_roots) == 1 and reloaded_axis_roots[0] is first_axis_root,
            "같은 Dataset Reload에서 기존 표시 축 Root가 교체되거나 복제되었습니다.",
        )
        assert_true(
            first_axis_root["colmap_display_forward_axis"] == "-Z"
            and first_axis_root["colmap_display_up_axis"] == "Y",
            "Reload 뒤 표시 축 설정이 유지되지 않았습니다.",
        )
        assert_true("UserObject" in bpy.data.objects, "비소유 Object가 Reload에서 삭제되었습니다.")
        assert_true("UserMesh" in bpy.data.meshes, "비소유 Object Data가 Reload에서 삭제되었습니다.")
        assert_true("UserCollection" in bpy.data.collections, "비소유 Collection이 Reload에서 삭제되었습니다.")
        assert_true(
            user_object.users_collection,
            "비소유 Object가 데이터블록에는 남았지만 장면 Collection 연결을 잃었습니다.",
        )
        assert_true(
            user_collection.users > 0,
            "비소유 Collection이 데이터블록에는 남았지만 장면 연결을 잃었습니다.",
        )
        assert_true(
            len(
                [
                    node_group
                    for node_group in bpy.data.node_groups
                    if node_group.get(RECORD_KIND_KEY) == "POINT_DISPLAY_NODES"
                ]
            )
            == 1,
            "Reload 뒤 이전 Point 표시 Node Group이 정리되지 않았습니다.",
        )
        assert_true(
            len(
                [
                    material
                    for material in bpy.data.materials
                    if material.get(RECORD_KIND_KEY) == "POINT_DISPLAY_MATERIAL"
                ]
            )
            == 1,
            "Reload 뒤 이전 Point 표시 Material이 정리되지 않았습니다.",
        )

        # 같은 Dataset transaction의 각 변경 경계에서 실패를 주입하고,
        # Object identity뿐 아니라 metadata·scale·잠금까지 원상 복구되는지 확인합니다.
        base_loaded = loaded_for_current(ensure_runtime(scene))
        fault_source = replace(
            base_loaded.source,
            source_fingerprint="f" * 64,
        )
        fault_loaded = replace(base_loaded, source=fault_source)
        _expect_transaction_rollback(
            scene,
            fault_loaded,
            "_apply_axis_update",
            "Axis Root 변경 뒤",
        )
        _expect_transaction_rollback(
            scene,
            fault_loaded,
            "_apply_point_update",
            "Point Mesh와 Node Group 교체 뒤",
        )
        _expect_transaction_rollback(
            scene,
            fault_loaded,
            "_apply_camera_updates",
            "기존 Camera 갱신 뒤",
        )

        source_model = base_loaded.reconstruction
        source_image = next(iter(source_model.images.values()))
        extra_image_id = max(source_model.images) + 1
        model_with_new_camera = Reconstruction(
            cameras=dict(source_model.cameras),
            images={
                **source_model.images,
                extra_image_id: replace(
                    source_image,
                    image_id=extra_image_id,
                    name="transaction_new_camera.jpg",
                ),
            },
            points3d=dict(source_model.points3d),
            rigs=dict(source_model.rigs),
            frames=dict(source_model.frames),
        )
        _expect_transaction_rollback(
            scene,
            replace(fault_loaded, reconstruction=model_with_new_camera),
            "_apply_camera_updates",
            "새 Camera 이동 뒤",
        )

        model_without_cameras = Reconstruction(
            cameras=dict(source_model.cameras),
            images={},
            points3d=dict(source_model.points3d),
            rigs=dict(source_model.rigs),
            frames=dict(source_model.frames),
        )
        _expect_transaction_rollback(
            scene,
            replace(fault_loaded, reconstruction=model_without_cameras),
            "_unlink_removed_cameras",
            "제거 Camera unlink 뒤",
        )

        # 성공 후 정리 실패는 기존 정책대로 commit을 유지하고 Warning으로 반환합니다.
        cleanup_staged = scene_bridge.build_staging(fault_loaded, 0.123, "X", "Z")
        original_cleanup = scene_transaction.cleanup_staging
        scene_transaction.cleanup_staging = (
            lambda _staged: (_ for _ in ()).throw(
                RuntimeError("의도한 staging cleanup 실패")
            )
        )
        try:
            cleanup_warnings = scene_bridge.commit_staging(scene, cleanup_staged)
        finally:
            scene_transaction.cleanup_staging = original_cleanup
            scene_bridge.cleanup_staging(cleanup_staged)
        assert_true(
            any("staging datablock 정리에 실패했습니다" in item for item in cleanup_warnings)
            and first_root[SOURCE_FINGERPRINT_KEY] == fault_source.source_fingerprint,
            "staging cleanup 실패가 성공한 commit을 유지하며 Warning을 반환하지 않았습니다.",
        )

        restore_staged = scene_bridge.build_staging(
            base_loaded,
            settings.point_size,
            settings.axis_forward,
            settings.axis_up,
        )
        scene_bridge.commit_staging(scene, restore_staged)

        orphan_staged = scene_bridge.build_staging(fault_loaded, 0.123, "X", "Z")
        original_orphan_cleanup = scene_transaction._remove_owned_orphan_data_resources
        scene_transaction._remove_owned_orphan_data_resources = (
            lambda: (_ for _ in ()).throw(
                RuntimeError("의도한 orphan cleanup 실패")
            )
        )
        try:
            orphan_warnings = scene_bridge.commit_staging(scene, orphan_staged)
        finally:
            scene_transaction._remove_owned_orphan_data_resources = (
                original_orphan_cleanup
            )
        assert_true(
            any("미사용 관리 datablock 정리에 실패했습니다" in item for item in orphan_warnings)
            and first_root[SOURCE_FINGERPRINT_KEY] == fault_source.source_fingerprint,
            "orphan cleanup 실패가 성공한 commit을 유지하며 Warning을 반환하지 않았습니다.",
        )

        restore_staged = scene_bridge.build_staging(
            base_loaded,
            settings.point_size,
            settings.axis_forward,
            settings.axis_up,
        )
        scene_bridge.commit_staging(scene, restore_staged)
        assert_true(
            first_root[SOURCE_FINGERPRINT_KEY] == base_loaded.source.source_fingerprint,
            "정리 실패 검증 뒤 권위 Scene metadata를 복구하지 못했습니다.",
        )

        # 0.1.2 이전 장면처럼 Axis Root가 없는 관리 구조도 같은 Object를
        # 유지한 채 표시 계층을 추가해야 합니다.
        point_basis_without_axis = first_point_object.matrix_basis.copy()
        camera_basis_without_axis = first_camera_object.matrix_basis.copy()
        bpy.data.objects.remove(first_axis_root, do_unlink=True)
        first_point_object.matrix_basis = point_basis_without_axis
        first_camera_object.matrix_basis = camera_basis_without_axis
        migrated = bpy.ops.colmap.import_model()
        assert_true(
            migrated == {"FINISHED"},
            f"Axis Root 없는 이전 장면 마이그레이션이 실패했습니다: {settings.status}",
        )
        migrated_axis_roots = [
            obj
            for obj in bpy.data.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "AXIS_ROOT"
        ]
        assert_true(
            len(migrated_axis_roots) == 1
            and first_point_object.parent is migrated_axis_roots[0]
            and first_camera_object.parent is migrated_axis_roots[0],
            "이전 장면 마이그레이션이 표시 축 Root와 부모 관계를 구성하지 못했습니다.",
        )
        assert_true(
            tuple(first_point_object.data.vertices[0].co) == (2.0, 3.0, 4.0),
            "이전 장면 마이그레이션이 원본 Point3D 좌표를 변경했습니다.",
        )
        active_uuid = settings.dataset_uuid

        empty_dataset = root / "empty_dataset"
        empty_dataset.mkdir()
        settings.dataset_root = str(empty_dataset)
        assert_true(not settings.sparse_directory, "후보가 없을 때 이전 Sparse 경로가 남았습니다.")
        assert_true(not settings.images_directory, "후보가 없을 때 이전 Images 경로가 남았습니다.")

        # 실패한 새 Import가 마지막 성공 장면과 활성 metadata를 보존하는지 확인합니다.
        settings.images_directory = str(images)
        settings.sparse_directory = str(root / "missing")
        try:
            failed = bpy.ops.colmap.import_model()
        except RuntimeError:
            failed = {"CANCELLED"}
        assert_true(failed == {"CANCELLED"}, "잘못된 source Import는 취소되어야 합니다.")
        owned_after = [
            collection
            for collection in scene.collection.children
            if collection.get(OWNER_KEY) == OWNER_TOKEN
            and collection.get(RECORD_KIND_KEY) == "ROOT"
        ]
        assert_true(len(owned_after) == 1, "실패 뒤 기존 관리 장면이 보존되지 않았습니다.")
        assert_true(owned_after[0][DATASET_UUID_KEY] == active_uuid, "실패 뒤 활성 dataset이 바뀌었습니다.")
        assert_true(settings.dataset_uuid == active_uuid, "실패 뒤 마지막 성공 metadata가 바뀌었습니다.")
finally:
    blender_colmap.unregister()

# 재등록 누수를 즉시 확인합니다.
blender_colmap.register()
blender_colmap.unregister()
assert_true(
    sync_point_scale_from_modifiers not in bpy.app.handlers.depsgraph_update_post,
    "Extension 해제 뒤 Point Scale 역동기화 핸들러가 남았습니다.",
)
print("BLENDER_COLMAP_BACKGROUND_IMPORT_OK")
