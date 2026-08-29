"""Modifier Camera Review의 3D Viewport 단축키 수명을 관리합니다."""

from __future__ import annotations

import bpy


_KEYMAP_ITEMS: list[tuple[bpy.types.KeyMap, bpy.types.KeyMapItem]] = []


def register_camera_review_keymap() -> None:
    """활성 Camera View 배경 전환을 F7 애드온 키맵으로 등록합니다."""

    if _KEYMAP_ITEMS:
        return
    window_manager = getattr(bpy.context, "window_manager", None)
    keyconfigs = getattr(window_manager, "keyconfigs", None)
    keyconfig = getattr(keyconfigs, "addon", None)
    if keyconfig is None:
        return
    keymap = keyconfig.keymaps.new(
        name="3D View",
        space_type="VIEW_3D",
        region_type="WINDOW",
    )
    item = keymap.keymap_items.new(
        "colmap.review_background_toggle",
        "F7",
        "PRESS",
        head=True,
    )
    _KEYMAP_ITEMS.append((keymap, item))


def unregister_camera_review_keymap() -> None:
    """등록한 Modifier F7 항목만 제거하고 사용자 키맵은 보존합니다."""

    for keymap, item in reversed(_KEYMAP_ITEMS):
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            pass
    _KEYMAP_ITEMS.clear()


__all__ = [
    "register_camera_review_keymap",
    "unregister_camera_review_keymap",
]
