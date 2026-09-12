"""데이터셋 폴더 선택창에서 표시 축을 함께 설정합니다."""

import os
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import axis_conversion_ensure

from .axis_display import (
    AXIS_ITEMS, CUSTOM_AXIS_PRESET, DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS, DEFAULT_UP_AXIS, axes_for_preset,
    axis_preset_items, axis_summary,
)
from .operation_guard import export_mutation_allowed, require_export_mutation_allowed


def _update_browser_preset(operator, _context):
    """선택창 내부의 축만 갱신하고 장면 설정은 확정 전까지 보존합니다."""
    axes = axes_for_preset(operator.axis_preset)
    if axes is not None:
        operator.axis_forward, operator.axis_up = axes


class COLMAP_OT_select_dataset_root(bpy.types.Operator):
    """폴더와 표시 축을 확정하면 기존 증분 가져오기를 즉시 시작합니다."""

    bl_idname = "colmap.select_dataset_root"
    bl_label = "Import"
    bl_description = "데이터셋 폴더와 표시 축을 선택하고 COLMAP 데이터를 가져옵니다"

    directory: StringProperty(name="Dataset Root", subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})
    axis_preset: EnumProperty(
        name="Preset", items=axis_preset_items(), default=DEFAULT_AXIS_PRESET,
        update=_update_browser_preset,
    )
    axis_forward: EnumProperty(name="Forward Axis", items=AXIS_ITEMS, default=DEFAULT_FORWARD_AXIS)
    axis_up: EnumProperty(name="Up Axis", items=AXIS_ITEMS, default=DEFAULT_UP_AXIS)

    @classmethod
    def poll(cls, context):
        """다른 데이터 변경 작업이 진행 중이면 폴더·축 변경을 막습니다."""
        return export_mutation_allowed(context)

    def invoke(self, context, _event):
        """현재 경로와 축을 임시 값으로 복사해 폴더 선택창을 엽니다."""
        settings = context.scene.colmap_import
        self._source_scene = context.scene
        self.directory = (bpy.path.abspath(settings.dataset_root) + os.sep) if settings.dataset_root else ""
        self.axis_preset = settings.axis_preset
        self.axis_forward = settings.axis_forward
        self.axis_up = settings.axis_up
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def check(self, _context):
        """사용자 정의 Forward와 Up이 같은 축이면 Blender 규칙으로 교정합니다."""
        return axis_conversion_ensure(self, "axis_forward", "axis_up")

    def draw(self, _context):
        """파일 탐색기 오른쪽에 표시 축과 원본 축 유지 안내를 표시합니다."""
        box = self.layout.box()
        box.label(text="Display Axis")
        box.prop(self, "axis_preset")
        if self.axis_preset == CUSTOM_AXIS_PRESET:
            box.prop(self, "axis_forward")
            box.prop(self, "axis_up")
        else:
            box.label(text=axis_summary(self.axis_forward, self.axis_up))
        box.label(text="Export: COLMAP 원본 축 유지", icon="INFO")

    def execute(self, context):
        """폴더와 축을 확정한 뒤 기존 Import 오퍼레이터의 진행창을 시작합니다."""
        try:
            require_export_mutation_allowed(context, "Dataset Root 선택")
            if getattr(self, "_source_scene", context.scene) != context.scene:
                raise ValueError("선택창을 연 장면이 변경되었습니다. 폴더를 다시 선택하십시오.")
            if not self.directory:
                raise ValueError("데이터셋 폴더를 선택하십시오.")
            directory = Path(bpy.path.abspath(self.directory)).expanduser().resolve()
            if not directory.is_dir():
                raise ValueError("선택한 데이터셋 폴더가 존재하지 않습니다.")
            self.check(context)
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings = context.scene.colmap_import
        settings.dataset_root = str(directory)
        settings.axis_preset = self.axis_preset
        if self.axis_preset == CUSTOM_AXIS_PRESET:
            settings.axis_forward = self.axis_forward
            settings.axis_up = self.axis_up
        try:
            result = bpy.ops.colmap.import_model("INVOKE_DEFAULT")
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        # 진행 모달은 기존 Import 오퍼레이터가 소유하고 파일 선택 오퍼레이터는 종료합니다.
        return {"FINISHED"} if result & {"RUNNING_MODAL", "FINISHED"} else {"CANCELLED"}


CLASSES = (COLMAP_OT_select_dataset_root,)
