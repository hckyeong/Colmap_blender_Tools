"""Projection Painting Blender Extension 진입점을 제공합니다."""

__version__ = "0.1.88"


def register():
    """Extension의 Blender 타입과 수명주기 기능을 등록합니다."""
    from .blender.lifecycle import register as register_impl

    register_impl()


def unregister():
    """Extension의 Blender 타입과 실행 중 자원을 역순으로 해제합니다."""
    from .blender.lifecycle import unregister as unregister_impl

    unregister_impl()


__all__ = ["__version__", "register", "unregister"]
