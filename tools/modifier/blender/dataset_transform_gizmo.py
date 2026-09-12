"""Modifier 전체 데이터의 행렬 미리보기와 전용 입력·피벗을 소유합니다."""

from math import atan2, exp, pi

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from .axis_display import apply_dataset_transform_preview, dataset_transform_roots, display_axis_matrix, set_transform_cancel_callback
from ..core.dataset_transform import compose_transforms, inverse_transform, transform_point
from .modifier_runtime_domain import domain_for_scene
from .modifier_session import SESSION, modifier_panel_poll

_ACTIVE = None
_PIVOTS = {}
_AXES = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}


def transform_in_progress(scene=None):
    """지정 장면에 아직 확정하지 않은 데이터 변환이 있는지 반환합니다."""
    return _ACTIVE is not None and (scene is None or _ACTIVE._scene == scene)


def cancel_active_transform(scene=None):
    """자신이 소유한 미리보기와 타이머만 해제하고 권위 행렬로 복원합니다."""
    if transform_in_progress(scene):
        _ACTIVE._release(restore=True)


def dataset_transform_allowed(context):
    """편집·대기 필터·복원·다른 트랜잭션이 없는 활성 전용 뷰인지 확인합니다."""
    return dataset_transform_block_reason(context) is None


def dataset_transform_block_reason(context):
    """전체 데이터 변환을 시작할 수 없는 정확한 현재 이유를 반환합니다."""
    if not modifier_panel_poll(context):
        return "활성 Modifier 뷰에서만 사용할 수 있습니다."
    if transform_in_progress():
        return "진행 중인 변환을 확정하거나 취소하십시오."
    from .camera_filter_queue import has_pending_filter_data
    from .modifier_input_guard import _modifier_input_lock_reason
    from .runtime_state import cached_runtime_matches_scene
    settings = context.scene.colmap_import
    domain = domain_for_scene(context.scene)
    if domain is None:
        return "먼저 COLMAP 데이터를 가져오십시오."
    if not domain.editable:
        return "데이터 오류를 복구한 뒤 변환하십시오."
    if getattr(context, "mode", "OBJECT") != "OBJECT" or settings.point_edit_mode_active or settings.camera_edit_mode_active:
        return "점 또는 카메라 편집 모드를 종료하십시오."
    if has_pending_filter_data(settings):
        return "대기 필터를 적용하거나 비우십시오."
    if not cached_runtime_matches_scene(context.scene):
        return "데이터 복원이 완료될 때까지 기다리십시오."
    return _modifier_input_lock_reason(context)


def _display(scene):
    """현재 장면의 원본 축에서 표시 축으로 가는 행렬을 반환합니다."""
    settings = scene.colmap_import
    return display_axis_matrix(settings.axis_forward, settings.axis_up)


def _pivot(scene):
    """삭제된 피벗을 원점으로 정리하고 현재 표시 공간 위치를 반환합니다."""
    domain = domain_for_scene(scene, required=True)
    state = domain.dataset_state
    point_id, raw = _PIVOTS.get(domain.domain_uuid, (None, (0.0, 0.0, 0.0)))
    if point_id is not None and point_id not in state.current_reconstruction.points3d:
        point_id, raw = None, (0.0, 0.0, 0.0)
        _PIVOTS[domain.domain_uuid] = point_id, raw
    elif point_id is not None:
        raw = tuple(state.current_reconstruction.points3d[point_id].xyz)
        _PIVOTS[domain.domain_uuid] = point_id, raw
    return Vector(transform_point(compose_transforms(_display(scene), state.dataset_transform), raw))


def _mouse(region, event):
    """창 좌표를 고정한 뷰포트 영역 좌표로 변환합니다."""
    return Vector((event.mouse_x - region.x, event.mouse_y - region.y))


def _pick_pivot(context, event):
    """원본 포인트를 화면 투영해 가까운 점의 영속 식별자와 원본 좌표를 저장합니다."""
    from .selection import managed_point_object
    domain = domain_for_scene(context.scene, required=True)
    state = domain.dataset_state
    obj = managed_point_object(context, state.dataset_uuid)
    if not obj.visible_get(view_layer=context.view_layer):
        return False
    region, view = context.region, context.space_data.region_3d
    mouse = _mouse(region, event)
    matrix = compose_transforms(_display(context.scene), state.dataset_transform)
    best = None
    best_key = (12.0 ** 2, float("inf"))
    for point_id, point in state.current_reconstruction.points3d.items():
        world = Vector(tuple(sum(matrix[i][j] * point.xyz[j] for j in range(3)) + matrix[i][3] for i in range(3)))
        clip = view.perspective_matrix @ world.to_4d()
        if clip.w <= 0 or not -clip.w <= clip.z <= clip.w:
            continue
        screen = view3d_utils.location_3d_to_region_2d(region, view, world)
        if screen is None:
            continue
        distance = (screen - mouse).length_squared
        key = distance, -(view.view_matrix @ world).z
        if key < best_key:
            best_key = key
            best = point_id, tuple(point.xyz), world
    if best is None:
        return False
    _PIVOTS[domain.domain_uuid] = best[0], best[1]
    view.view_location = best[2]
    context.area.tag_redraw()
    return True


def route_dataset_transform_input(context, event):
    """실제 Modifier WINDOW 위의 G/R/S와 일반 탐색 더블클릭만 소비합니다."""
    from .modifier_input_guard import _area_and_region_under_mouse, _owned_modifier_screen_is_active
    if transform_in_progress() or not _owned_modifier_screen_is_active(context):
        return False
    if getattr(context, "region_popup", None) is not None:
        return False
    if any(getattr(event, name, False) for name in ("ctrl", "shift", "alt", "oskey", "hyper")):
        return False
    mode = {"G": "TRANSLATE", "R": "ROTATE", "S": "SCALE"}.get(event.type)
    double = event.type == "LEFTMOUSE" and event.value == "DOUBLE_CLICK"
    if not double and (mode is None or event.value != "PRESS" or getattr(event, "is_repeat", False)):
        return False
    area, region = _area_and_region_under_mouse(context, event)
    if getattr(area, "type", "") != "VIEW_3D" or getattr(region, "type", "") != "WINDOW":
        return False
    with context.temp_override(area=area, region=region):
        if not dataset_transform_allowed(context):
            # 전용 뷰의 금지된 변환이 Blender 기본 G/R/S로 빠져나가지 않게 소비합니다.
            return not double
        if double:
            return _pick_pivot(context, event)
        return "RUNNING_MODAL" in bpy.ops.colmap.transform_dataset("INVOKE_DEFAULT", mode=mode)


class COLMAP_OT_transform_dataset(bpy.types.Operator):
    """점군과 카메라의 공통 행렬을 미리보고 하나의 도메인 명령으로 확정합니다."""
    bl_idname = "colmap.transform_dataset"
    bl_label = "데이터 전체 변환"
    bl_options = {"INTERNAL", "BLOCKING"}
    mode: EnumProperty(items=(("TRANSLATE", "이동", ""), ("ROTATE", "회전", ""), ("SCALE", "배율", "")))
    axis: StringProperty(default="")
    from_gizmo: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        """활성 Modifier 제어 뷰에서 준비된 데이터만 변환하도록 제한합니다."""
        return dataset_transform_allowed(context)

    def invoke(self, context, event):
        """원본 행렬과 뷰·도메인 식별자를 고정한 뒤 미리보기를 시작합니다."""
        if not self.poll(context):
            return {"CANCELLED"}
        region = next((r for r in context.area.regions if r.type == "WINDOW"), None)
        if region is None:
            return {"CANCELLED"}
        from_panel = context.region.type != "WINDOW"
        with context.temp_override(region=region):
            return self._begin(context, event, from_panel)

    def _begin(self, context, event, from_panel):
        """실제 WINDOW 문맥에서 모달을 등록하고 패널 입력의 첫 이동을 대기합니다."""
        global _ACTIVE
        self._scene = context.scene
        self._area = context.area
        self._window = context.window
        self._region = next((r for r in context.area.regions if r.type == "WINDOW"), None)
        self._view = context.space_data.region_3d
        if self._region is None or self._view is None:
            return {"CANCELLED"}
        self._domain = domain_for_scene(context.scene, required=True)
        self._roots = dataset_transform_roots(context.scene)
        self._identity = self._domain.identity
        self._session_generation = SESSION.generation
        self._display = _display(context.scene)
        self._start = self._domain.dataset_state.dataset_transform
        self._preview = self._start
        self._pivot = _pivot(context.scene)
        self._start_mouse = _mouse(self._region, event)
        self._awaiting_viewport = from_panel
        self._manager = context.window_manager
        self._timer = self._manager.event_timer_add(0.1, window=context.window)
        _ACTIVE = self
        self._domain.transform_preview_active = True
        try:
            self._manager.modal_handler_add(self)
        except Exception:
            self._release(restore=True)
            raise
        return {"RUNNING_MODAL"}

    def _current(self, context):
        """창·세션·축·도메인 권위가 시작 상태와 같은지 검증합니다."""
        from .camera_filter_queue import has_pending_filter_data
        from .import_progress_session import active_import_progress_session
        from .export_progress_session import active_export_progress_session

        try:
            settings = self._scene.colmap_import
            return bool(
                _ACTIVE is self and context.window == self._window
                and context.scene == self._scene and modifier_panel_poll(context)
                and SESSION.generation == self._session_generation
                and context.area == self._area
                and domain_for_scene(self._scene) is self._domain
                and self._domain.identity == self._identity
                and self._domain.editable and _display(self._scene) == self._display
                and not settings.point_edit_mode_active and not settings.camera_edit_mode_active
                and not settings.export_running and not has_pending_filter_data(settings)
                and active_import_progress_session() is None
                and active_export_progress_session() is None
            )
        except (ReferenceError, RuntimeError):
            return False

    def _update(self, event):
        """화면 입력의 월드 증분을 원본 공간으로 켤레 변환해 루트에만 표시합니다."""
        mouse = _mouse(self._region, event)
        if self._awaiting_viewport:
            if 0 <= mouse.x < self._region.width and 0 <= mouse.y < self._region.height:
                self._start_mouse = mouse
                self._awaiting_viewport = False
            return
        start = self._start_mouse
        region, view, pivot = self._region, self._view, self._pivot
        if self.mode == "TRANSLATE":
            a = view3d_utils.region_2d_to_location_3d(region, view, start, pivot)
            b = view3d_utils.region_2d_to_location_3d(region, view, mouse, pivot)
            delta_vector = b - a
            if self.axis in _AXES:
                axis = _AXES[self.axis]
                p = view3d_utils.location_3d_to_region_2d(region, view, pivot)
                q = view3d_utils.location_3d_to_region_2d(region, view, pivot + axis)
                if p is None or q is None or (q - p).length_squared < 1.0e-8:
                    return
                delta_vector = axis * ((mouse - start).dot(q - p) / (q - p).length_squared)
            delta = Matrix.Translation(delta_vector)
        else:
            center = view3d_utils.location_3d_to_region_2d(region, view, pivot)
            if self.mode == "SCALE":
                # 중앙 핸들도 영의 반경 없이 양수 균일 배율을 만들도록 수평 이동을 사용합니다.
                ratio = exp(max(-13.0, min(13.0, (mouse.x - start.x) * 0.01)))
                local = Matrix.Scale(ratio, 4)
            else:
                if center is None:
                    return
                a, b = start - center, mouse - center
                angle = ((mouse.x - start.x) * 0.01 if a.length < 8.0
                         else atan2(a.x * b.y - a.y * b.x, a.dot(b)))
                axis = _AXES.get(self.axis, view.view_rotation @ Vector((0, 0, 1)))
                if (view.view_rotation @ Vector((0, 0, 1))).dot(axis) < 0:
                    angle = -angle
                local = Matrix.Rotation(angle, 4, axis)
            delta = Matrix.Translation(pivot) @ local @ Matrix.Translation(-pivot)
        self._preview = compose_transforms(
            compose_transforms(inverse_transform(self._display), delta),
            compose_transforms(self._display, self._start),
        )
        apply_dataset_transform_preview(self._scene, self._preview, roots=self._roots)
        self._area.tag_redraw()

    def _release(self, *, restore):
        """타이머와 활성 소유권을 회수하고 요청 시 확정 행렬을 다시 표시합니다."""
        global _ACTIVE
        if _ACTIVE is not self:
            return
        _ACTIVE = None
        self._domain.transform_preview_active = False
        try:
            if self._timer is not None:
                try:
                    self._manager.event_timer_remove(self._timer)
                except (ReferenceError, RuntimeError):
                    pass
                self._timer = None
        finally:
            try:
                if restore and domain_for_scene(self._scene) is self._domain:
                    apply_dataset_transform_preview(self._scene)
                self._area.tag_redraw()
            except ReferenceError:
                # 파일 교체로 파괴된 RNA는 복원할 표시 자원 자체가 없습니다.
                pass

    def _finish(self, context, commit):
        """미리보기 잠금을 해제하고 원자적 도메인 명령 또는 취소를 실행합니다."""
        if not commit:
            self._release(restore=True)
            return {"CANCELLED"}
        self._release(restore=True)
        try:
            from .edit_workflows import commit_dataset_transform
            commit_dataset_transform(context, tuple(tuple(float(v) for v in row) for row in self._preview))
        except Exception as error:
            apply_dataset_transform_preview(self._scene)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}

    def modal(self, context, event):
        """핸들·키보드별 확정과 축 제한, 취소 및 포커스 상실을 처리합니다."""
        if event.type == "WINDOW_DEACTIVATE" or not self._current(context):
            return self._finish(context, False)
        if event.type in {"MOUSEMOVE", "LEFTMOUSE"} and not self._awaiting_viewport:
            mouse = _mouse(self._region, event)
            if not (0 <= mouse.x < self._region.width and 0 <= mouse.y < self._region.height):
                return self._finish(context, False)
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            return self._finish(context, False)
        if event.type in {"RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            return self._finish(context, True)
        if event.type == "LEFTMOUSE" and event.value == ("RELEASE" if self.from_gizmo else "PRESS"):
            return self._finish(context, True)
        try:
            if event.type in _AXES and event.value == "PRESS":
                self.axis = "" if self.axis == event.type else event.type
                self._update(event)
            elif event.type == "MOUSEMOVE":
                self._update(event)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return self._finish(context, False)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender의 강제 종료에서도 자신이 소유한 미리보기를 복원합니다."""
        self._release(restore=True)


class COLMAP_OT_reset_dataset_pivot(bpy.types.Operator):
    """데이터 변환의 피벗과 현재 뷰 회전 중심을 원본 원점으로 되돌립니다."""
    bl_idname = "colmap.reset_dataset_pivot"
    bl_label = "피벗 원점 초기화"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """변환을 시작할 수 있을 때만 피벗 초기화를 허용합니다."""
        return dataset_transform_allowed(context)

    def execute(self, context):
        """영속 점 참조를 버리고 원본 원점을 현재 표시 축으로 배치합니다."""
        domain = domain_for_scene(context.scene, required=True)
        _PIVOTS.pop(domain.domain_uuid, None)
        context.space_data.region_3d.view_location = _pivot(context.scene)
        context.area.tag_redraw()
        return {"FINISHED"}


class COLMAP_GGT_dataset_transform(bpy.types.GizmoGroup):
    """전체 데이터에 월드 XYZ 화살표·회전 링과 중앙 균일 배율을 제공합니다."""
    bl_idname = "COLMAP_GGT_dataset_transform"
    bl_label = "데이터 전체 변환"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        """전용 WINDOW에서 표시 옵션이 켜지고 편집이 가능할 때만 보입니다."""
        return bool(getattr(context.region, "type", "") == "WINDOW"
                    and dataset_transform_allowed(context)
                    and context.scene.colmap_import.show_transform_gizmo)

    def _bind(self, gizmo, mode, axis=""):
        """각 핸들을 동일한 전용 모달과 놓기 확정 규칙으로 연결합니다."""
        operator = gizmo.target_set_operator("colmap.transform_dataset")
        operator.mode, operator.axis, operator.from_gizmo = mode, axis, True

    def setup(self, context):
        """점군 자원에 접근하지 않고 일곱 개의 작은 핸들만 생성합니다."""
        self._handles = []
        for axis, color in zip("XYZ", ((0.9, 0.2, 0.2), (0.2, 0.9, 0.2), (0.2, 0.4, 0.95))):
            for kind, mode, size in (("arrow", "TRANSLATE", 0.75), ("dial", "ROTATE", 0.9)):
                handle = self.gizmos.new(f"GIZMO_GT_{kind}_3d")
                handle.color, handle.alpha, handle.scale_basis = color, 0.7, size
                self._bind(handle, mode, axis)
                self._handles.append((axis, handle))
        handle = self.gizmos.new("GIZMO_GT_move_3d")
        handle.color, handle.alpha, handle.scale_basis = (0.95, 0.8, 0.15), 0.85, 0.2
        self._bind(handle, "SCALE")
        self._handles.append(("", handle))

    def draw_prepare(self, context):
        """피벗의 작은 행렬만 갱신하며 원본 점군 배열은 유지합니다."""
        pivot = _pivot(context.scene)
        for axis, handle in self._handles:
            rotation = {"X": Matrix.Rotation(pi / 2, 4, "Y"), "Y": Matrix.Rotation(-pi / 2, 4, "X")}.get(axis, Matrix.Identity(4))
            handle.matrix_basis = Matrix.Translation(pivot) @ rotation


@persistent
def _before_load(_unused):
    """파일을 교체하기 전에 미리보기와 임시 피벗 참조를 정리합니다."""
    cancel_active_transform()
    _PIVOTS.clear()


def register_transform_runtime():
    """파일 교체 전 취소 처리를 한 번만 등록합니다."""
    from .modifier_input_guard import set_transform_input_callbacks

    set_transform_input_callbacks(route_dataset_transform_input, cancel_active_transform)
    set_transform_cancel_callback(cancel_active_transform)
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)


def unregister_transform_runtime():
    """등록 해제 시 자체 미리보기·피벗·파일 처리기만 정리합니다."""
    from .modifier_input_guard import set_transform_input_callbacks

    cancel_active_transform()
    set_transform_input_callbacks(None, None)
    set_transform_cancel_callback(None)
    _PIVOTS.clear()
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)


CLASSES = (COLMAP_OT_transform_dataset, COLMAP_OT_reset_dataset_pivot, COLMAP_GGT_dataset_transform)
