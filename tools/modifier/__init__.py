"""COLMAP Modifier Extension 진입점입니다."""

__version__ = "0.17.4"


def register():
    # 순수 Python core가 Blender 밖에서 import될 때 bpy를 요구하지 않습니다.
    from .blender.lifecycle import register as register_impl

    register_impl()


def unregister():
    from .blender.lifecycle import unregister as unregister_impl

    unregister_impl()


__all__ = ["__version__", "register", "unregister"]
