"""실제 Blender의 행렬·연산자·등록 경계에서 전체 데이터 변환 입력을 검증합니다."""

import sys
import tempfile
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

import bpy
from mathutils import Vector

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier
from tools.modifier.blender import dataset_transform_gizmo as gizmo
from tools.modifier.blender.modifier_runtime_domain import domain_for_scene
from tools.modifier.blender.ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY
from tests.modifier.fixture_utils import make_legacy_text_multi_point


def check(value, message):
    """실제 Blender 검증이 실패하면 명확한 오류를 발생시킵니다."""
    if not value:
        raise AssertionError(message)


def event(kind, value="PRESS", x=100, y=100, **extra):
    """수동 모달 검증에 필요한 실제 이벤트 필드를 제공합니다."""
    values = dict(type=kind, value=value, mouse_x=x, mouse_y=y,
                  ctrl=False, shift=False, alt=False, oskey=False, hyper=False, is_repeat=False)
    values.update(extra)
    return SimpleNamespace(**values)


def start(mode, from_gizmo=False):
    """배경 모드에 없는 이벤트 루프만 대역으로 두고 실제 제품 모달 메서드를 시작합니다."""
    operator = SimpleNamespace(mode=mode, axis="", from_gizmo=from_gizmo,
                               report=lambda levels, message: print(message))
    for name in ("_begin", "_update", "_release", "_finish", "_current", "modal"):
        setattr(operator, name, MethodType(getattr(gizmo.COLMAP_OT_transform_dataset, name), operator))
    manager = SimpleNamespace(event_timer_add=lambda interval, window: object(),
                              event_timer_remove=lambda timer: None,
                              modal_handler_add=lambda value: None)
    context = SimpleNamespace(**{name: getattr(bpy.context, name) for name in
                                  ("scene", "area", "window", "region", "space_data")}, window_manager=manager)
    return operator._begin(context, event("S"), False)


modifier.register()
try:
    gizmo.register_transform_runtime()
    check(bpy.app.handlers.load_pre.count(gizmo._before_load) == 1, "파일 처리기가 중복 등록되었습니다.")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        make_legacy_text_multi_point(root)
        settings = bpy.context.scene.colmap_import
        settings.dataset_root, settings.input_format = str(root), "TEXT"
        check(bpy.ops.colmap.import_model() == {"FINISHED"}, settings.status)
        window = bpy.context.window
        area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
        region = next(region for region in area.regions if region.type == "WINDOW")
        domain = domain_for_scene(bpy.context.scene, required=True)
        point_mesh = next(obj.data for obj in bpy.context.scene.objects
                          if obj.get(OWNER_KEY) == OWNER_TOKEN and obj.get(RECORD_KIND_KEY) == "POINT_MESH")
        mesh_pointer = point_mesh.as_pointer()
        with bpy.context.temp_override(area=area, region=region), patch.object(gizmo, "modifier_panel_poll", return_value=True):
            check(start("SCALE", from_gizmo=True) == {"RUNNING_MODAL"}, "배율 모달 시작 실패")
            active = gizmo._ACTIVE
            check(active is not None and domain.transform_preview_active, "미리보기 잠금이 없습니다.")
            active._start_mouse = Vector((100, 100))
            active._update(event("MOUSEMOVE", x=region.x + 120, y=region.y + 100))
            check(active._preview[0][0] > 1, "실제 배율 행렬이 증가하지 않았습니다.")
            check(point_mesh.as_pointer() == mesh_pointer, "미리보기가 점군 Mesh를 복사했습니다.")
            revision = domain.dataset_state.edit_revision
            check(active.modal(bpy.context, event("LEFTMOUSE", x=region.x + 120, y=region.y + 100)) == {"RUNNING_MODAL"}, "핸들이 누르기에서 확정되었습니다.")
            check(active.modal(bpy.context, event("LEFTMOUSE", "RELEASE", x=region.x + 120, y=region.y + 100)) == {"FINISHED"}, "핸들이 놓기에서 확정되지 않았습니다.")
            check(not domain.transform_preview_active and domain.dataset_state.edit_revision == revision + 1, "확정 잠금 또는 이력 오류")
            committed = domain.dataset_state.dataset_transform
            check(start("SCALE") == {"RUNNING_MODAL"}, "취소 검증 시작 실패")
            active = gizmo._ACTIVE
            active._start_mouse = Vector((100, 100))
            active._update(event("MOUSEMOVE", x=region.x + 140, y=region.y + 100))
            check(active.modal(bpy.context, event("ESC")) == {"CANCELLED"}, "취소 키 처리 실패")
            check(domain.dataset_state.dataset_transform == committed and not domain.transform_preview_active, "취소가 권위 행렬 또는 잠금을 손상했습니다.")
            check(start("ROTATE") == {"RUNNING_MODAL"}, "패널 기준점 검증 시작 실패")
            active = gizmo._ACTIVE
            active._awaiting_viewport = True
            active._update(event("MOUSEMOVE", x=region.x + 80, y=region.y + 80))
            check(not active._awaiting_viewport and active._preview == committed, "첫 뷰 진입이 회전 값을 바꿨습니다.")
            from bpy_extras import view3d_utils

            center = view3d_utils.location_3d_to_region_2d(region, active._view, active._pivot)
            check(center is not None, "회전 피벗을 투영하지 못했습니다.")
            active.axis = "Z"
            active._start_mouse = center + Vector((40, 0))
            active._update(event("MOUSEMOVE", x=region.x + center.x, y=region.y + center.y + 40))
            check(active._preview != committed, "회전 링 입력이 행렬을 갱신하지 않았습니다.")
            gizmo.cancel_active_transform()
            with patch("tools.modifier.blender.modifier_input_guard._owned_modifier_screen_is_active", return_value=True), patch("tools.modifier.blender.modifier_input_guard._area_and_region_under_mouse", return_value=(area, region)), patch.object(gizmo, "dataset_transform_allowed", return_value=False):
                for key in "GRS":
                    check(gizmo.route_dataset_transform_input(bpy.context, event(key)), "금지된 변환 키가 기본 Blender로 빠졌습니다.")
                check(not gizmo.route_dataset_transform_input(bpy.context, event("G", ctrl=True)), "보조키 조합을 가로챘습니다.")
            ui_region = next(region for region in area.regions if region.type == "UI")
            with patch("tools.modifier.blender.modifier_input_guard._owned_modifier_screen_is_active", return_value=True), patch("tools.modifier.blender.modifier_input_guard._area_and_region_under_mouse", return_value=(area, ui_region)):
                check(not gizmo.route_dataset_transform_input(bpy.context, event("G")), "패널 텍스트 입력을 가로챘습니다.")
    print("데이터 변환 기즈모 배경 검증 통과")
finally:
    gizmo.cancel_active_transform()
    modifier.unregister()
    check(gizmo._before_load not in bpy.app.handlers.load_pre, "파일 처리기가 남았습니다.")
