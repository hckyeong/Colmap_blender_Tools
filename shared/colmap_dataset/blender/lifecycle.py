"""공용 COLMAP Dataset Scene Property의 등록과 해제를 관리합니다."""

from __future__ import annotations

import bpy
from bpy.props import PointerProperty

from .properties import CLASSES, COLMAP_PG_dataset
from .registry import clear_display_listeners


_REGISTERED = False


def register() -> None:
    """공용 Dataset PropertyGroup과 Scene.colmap_dataset을 한 번 등록합니다."""

    global _REGISTERED
    if _REGISTERED:
        return
    registered_classes: list[type] = []
    property_registered = False
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered_classes.append(cls)
        bpy.types.Scene.colmap_dataset = PointerProperty(type=COLMAP_PG_dataset)
        property_registered = True
        _REGISTERED = True
    except Exception:
        if property_registered and hasattr(bpy.types.Scene, "colmap_dataset"):
            del bpy.types.Scene.colmap_dataset
        for cls in reversed(registered_classes):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
        raise


def unregister() -> None:
    """공용 Dataset Listener, Scene Property와 Class를 역순으로 해제합니다."""

    global _REGISTERED
    if not _REGISTERED:
        return
    _REGISTERED = False
    clear_display_listeners()
    if hasattr(bpy.types.Scene, "colmap_dataset"):
        del bpy.types.Scene.colmap_dataset
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


def is_registered() -> bool:
    """공용 Dataset Scene Property의 등록 완료 여부를 반환합니다."""

    return _REGISTERED


__all__ = ["is_registered", "register", "unregister"]
