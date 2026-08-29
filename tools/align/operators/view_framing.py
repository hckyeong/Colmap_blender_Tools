"""A/P 뷰포트 전용 Frame All 기즈모와 마우스·메뉴 진입점을 제공합니다."""

from __future__ import annotations

from typing import Any

import bpy

from ..constants import ROLE_MOVING, ROLE_REFERENCE
from ..session import is_session_active_for_window
from ..state import RUNTIME
from ..status import set_window_status_text
from ..viewport.picking import find_viewport_under_mouse
from ..viewport.registry import role_for_area
from ..viewport.renderer import render_model_ids_for_role
from .workspace import WorkspaceConfigurationError, fit_role_view


_ROLE_LABELS = {
    ROLE_REFERENCE: "A",
    ROLE_MOVING: "P",
}


def _role_for_context(context: Any) -> str | None:
    """현재 컨텍스트가 활성 세션의 A/P WINDOW 뷰포트인지 판별합니다."""
    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    region = getattr(context, "region", None)
    if (
        window is None
        or area is None
        or getattr(area, "type", None) != "VIEW_3D"
        or region is None
        or getattr(region, "type", None) != "WINDOW"
    ):
        return None
    role = role_for_area(window, area)
    if role not in {ROLE_REFERENCE, ROLE_MOVING}:
        return None
    if not is_session_active_for_window(window):
        return None
    return role


def _set_frame_status(window: Any, role: str, message: str) -> None:
    """Frame All 결과를 카탈로그와 해당 Window 상태 표시줄에 함께 기록합니다."""
    RUNTIME.model_catalog.message = message
    set_window_status_text(window, message)


def frame_visible_role_under_mouse(context: Any, event: Any) -> bool:
    """마우스 아래 실제 A/P WINDOW만 찾아 해당 역할의 표시 집합을 맞춥니다."""
    window = getattr(context, "window", None)
    if window is None or not is_session_active_for_window(window):
        return False
    mouse_x = getattr(event, "mouse_x", None)
    mouse_y = getattr(event, "mouse_y", None)
    if mouse_x is None or mouse_y is None:
        return False
    viewport = find_viewport_under_mouse(window, int(mouse_x), int(mouse_y))
    if viewport is None:
        return False
    _area, _region, role = viewport
    if role not in {ROLE_REFERENCE, ROLE_MOVING}:
        return False
    role_label = _ROLE_LABELS[role]
    try:
        fitted = fit_role_view(window, role)
    except WorkspaceConfigurationError as error:
        _set_frame_status(
            window,
            role,
            f"{role_label} 뷰 화면 맞춤을 취소했습니다: {error}",
        )
        return True
    if not fitted:
        _set_frame_status(
            window,
            role,
            f"{role_label} 뷰에 화면 맞춤할 보이는 레이어가 없습니다.",
        )
    else:
        _set_frame_status(window, role, f"{role_label} 그룹의 보이는 레이어 전체를 맞췄습니다.")
    return True


def draw_frame_all_view_menu(self: Any, context: Any) -> None:
    """현재 A/P 뷰에서만 역할별 GPU 데이터 Frame All 메뉴 항목을 그립니다."""
    window = getattr(context, "window", None)
    if window is None or not is_session_active_for_window(window):
        return
    role = role_for_area(window, getattr(context, "area", None))
    if role not in {ROLE_REFERENCE, ROLE_MOVING}:
        return
    role_label = _ROLE_LABELS[role]
    operator = self.layout.operator(
        "colmap_align.fit_view",
        text=f"{role_label} 그룹 전체 보기",
        icon="HOME",
    )
    operator.role = role
    self.layout.separator()


class COLMAPALIGN_GGT_frame_all(bpy.types.GizmoGroup):
    """활성 A/P 뷰포트 오른쪽 아래에 보이는 레이어 전체 맞춤 버튼을 제공합니다."""

    bl_idname = "COLMAPALIGN_GGT_frame_all"
    bl_label = "COLMAP Align Frame All"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"PERSISTENT", "SCALE"}

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션의 A/P WINDOW Region에서만 기즈모를 허용합니다."""
        return _role_for_context(context) is not None

    def setup(self, context):
        """내장 Home 아이콘 버튼 하나를 기존 역할 화면 맞춤 오퍼레이터에 연결합니다."""
        button = self.gizmos.new("GIZMO_GT_button_2d")
        button.draw_options = {"BACKDROP", "OUTLINE"}
        button.icon = "HOME"
        button.color = (0.55, 0.55, 0.55)
        button.alpha = 0.75
        button.color_highlight = (0.9, 0.9, 0.9)
        button.alpha_highlight = 0.95
        button.backdrop_fill_alpha = 0.18
        button.line_width = 1.5
        button.show_drag = False
        button.scale_basis = 14.0
        button.use_tooltip = True
        operator = button.target_set_operator("colmap_align.fit_view")
        role = _role_for_context(context)
        if role is not None and operator is not None:
            operator.role = role
        self._frame_all_button = button

    def draw_prepare(self, context):
        """버튼 위치와 숨김 상태만 갱신하고 경계·GPU 배치는 계산하지 않습니다."""
        button = getattr(self, "_frame_all_button", None)
        if button is None:
            return
        role = _role_for_context(context)
        target_ids = render_model_ids_for_role(role) if role is not None else ()
        button.hide = not bool(target_ids)
        button.scale_basis = 14.0
        region = getattr(context, "region", None)
        if region is None:
            return
        button.matrix_basis[0][3] = max(float(region.width) - 28.0, 28.0)
        button.matrix_basis[1][3] = 28.0
        button.matrix_basis[2][3] = 0.0


CLASSES = (COLMAPALIGN_GGT_frame_all,)
