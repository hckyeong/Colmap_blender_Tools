"""세 독립 COLMAP 도구를 하나의 Blender Extension으로 등록합니다."""

__version__ = "0.3.72"


def register() -> None:
    """Root Shell과 세 독립 도구를 순서대로 등록합니다."""

    from .shell.lifecycle import register as register_extension

    register_extension()


def unregister() -> None:
    """세 독립 도구와 Root Shell을 등록 역순으로 해제합니다."""

    from .shell.lifecycle import unregister as unregister_extension

    unregister_extension()


__all__ = ["__version__", "register", "unregister"]
