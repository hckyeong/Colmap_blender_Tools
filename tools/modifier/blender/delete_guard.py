"""COLMAP 선택 모드에서 Blender 기본 X/Delete 삭제를 차단합니다."""

from __future__ import annotations

import bpy


_KEYMAP_ITEMS: list[tuple[bpy.types.KeyMap, bpy.types.KeyMapItem]] = []


def _warning_message(settings) -> str:
    if settings.point_edit_mode_active:
        return (
            "Edit Point Mode에서는 X/Delete로 정점을 삭제할 수 없습니다. "
            "COLMAP 패널의 Remove 버튼을 사용하십시오."
        )
    return (
        "Edit Camera Mode에서는 X/Delete로 Camera를 삭제할 수 없습니다. "
        "COLMAP 패널의 Remove 버튼을 사용하십시오."
    )


class COLMAP_OT_block_native_delete(bpy.types.Operator):
    bl_idname = "colmap.block_native_delete"
    bl_label = "COLMAP Delete 차단"
    bl_description = "COLMAP 선택 모드의 기본 삭제를 막고 Remove 사용을 안내합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and (
                settings.point_edit_mode_active
                or settings.camera_edit_mode_active
            )
        )

    def execute(self, context):
        settings = context.scene.colmap_import
        message = _warning_message(settings)
        settings.point_selection_status = message

        def draw_warning(menu, _context):
            menu.layout.label(text=message, icon="ERROR")

        if not bpy.app.background and context.window is not None:
            context.window_manager.popup_menu(
                draw_warning,
                title="COLMAP 삭제 차단",
                icon="ERROR",
            )
            self.report({"WARNING"}, message)
        return {"FINISHED"}


def register_delete_guard_keymaps() -> None:
    if _KEYMAP_ITEMS:
        return
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return
    for keymap_name in ("Mesh", "Object Mode"):
        keymap = keyconfig.keymaps.new(
            name=keymap_name,
            space_type="EMPTY",
            region_type="WINDOW",
        )
        for event_type in ("X", "DEL"):
            item = keymap.keymap_items.new(
                COLMAP_OT_block_native_delete.bl_idname,
                event_type,
                "PRESS",
                head=True,
            )
            _KEYMAP_ITEMS.append((keymap, item))


def unregister_delete_guard_keymaps() -> None:
    for keymap, item in reversed(_KEYMAP_ITEMS):
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError):
            pass
    _KEYMAP_ITEMS.clear()


CLASSES = (COLMAP_OT_block_native_delete,)
