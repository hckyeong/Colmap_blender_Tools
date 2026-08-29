"""Root Shell과 세 독립 도구의 등록·해제 순서를 관리합니다."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Callable

import bpy

from .menu import CLASSES as MENU_CLASSES
from .menu import draw_window_menu
from .preferences import CLASSES as PREFERENCE_CLASSES


_TOOL_MODULE_NAMES = (
    ".tools.align",
    ".tools.modifier",
    ".tools.projection_paint",
)
_SHELL_CLASSES = PREFERENCE_CLASSES + MENU_CLASSES
_REGISTERED_TOOL_MODULES: list[ModuleType] = []
_REGISTERED_SHELL_CLASSES: list[type] = []
_WINDOW_MENU_REGISTERED = False
_REGISTERED = False


def _root_package_name() -> str:
    """현재 설치 Repository를 포함한 Root Extension 패키지명을 반환합니다."""

    return __package__.rsplit(".shell", 1)[0]


def _load_tool_modules() -> tuple[ModuleType, ...]:
    """고정된 세 독립 도구 패키지를 Root Namespace에서 불러옵니다."""

    package_name = _root_package_name()
    return tuple(import_module(name, package=package_name) for name in _TOOL_MODULE_NAMES)


def _tool_callback(module: ModuleType, name: str) -> Callable[[], None]:
    """도구가 공개한 등록 또는 해제 Callback을 검증해 반환합니다."""

    callback = getattr(module, name, None)
    if not callable(callback):
        raise RuntimeError(f"독립 도구가 {name}()을 제공하지 않습니다: {module.__name__}")
    return callback


def _register_window_menu() -> None:
    """공통 Window 메뉴 Callback을 정확히 한 번 등록합니다."""

    global _WINDOW_MENU_REGISTERED
    if _WINDOW_MENU_REGISTERED:
        return
    bpy.types.TOPBAR_MT_window.append(draw_window_menu)
    _WINDOW_MENU_REGISTERED = True


def _unregister_window_menu() -> None:
    """Root Shell이 등록한 Window 메뉴 Callback만 제거합니다."""

    global _WINDOW_MENU_REGISTERED
    if not _WINDOW_MENU_REGISTERED:
        return
    try:
        bpy.types.TOPBAR_MT_window.remove(draw_window_menu)
    except (AttributeError, RuntimeError, ValueError):
        pass
    _WINDOW_MENU_REGISTERED = False


def _rollback_registration() -> None:
    """부분 등록된 도구와 Shell Class를 역순으로 정리합니다."""

    _unregister_window_menu()
    for module in reversed(_REGISTERED_TOOL_MODULES):
        try:
            _tool_callback(module, "unregister")()
        except Exception:
            pass
    _REGISTERED_TOOL_MODULES.clear()
    for cls in reversed(_REGISTERED_SHELL_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    _REGISTERED_SHELL_CLASSES.clear()


def register() -> None:
    """Shell Class와 세 독립 도구를 원자적으로 등록합니다."""

    global _REGISTERED
    if _REGISTERED:
        return
    try:
        for cls in _SHELL_CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED_SHELL_CLASSES.append(cls)
        for module in _load_tool_modules():
            _tool_callback(module, "register")()
            _REGISTERED_TOOL_MODULES.append(module)
        _register_window_menu()
        _REGISTERED = True
    except Exception:
        _REGISTERED = False
        _rollback_registration()
        raise


def unregister() -> None:
    """공통 메뉴와 세 도구 및 Shell Class를 역순으로 모두 해제합니다."""

    global _REGISTERED
    if not _REGISTERED and not _REGISTERED_TOOL_MODULES and not _REGISTERED_SHELL_CLASSES:
        return
    _REGISTERED = False
    failures: list[BaseException] = []
    _unregister_window_menu()
    for module in reversed(_REGISTERED_TOOL_MODULES):
        try:
            _tool_callback(module, "unregister")()
        except BaseException as error:
            failures.append(error)
    _REGISTERED_TOOL_MODULES.clear()
    for cls in reversed(_REGISTERED_SHELL_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except BaseException as error:
            failures.append(error)
    _REGISTERED_SHELL_CLASSES.clear()
    if failures:
        raise RuntimeError("COLMAP Tools 해제 중 하나 이상의 자원 정리에 실패했습니다.") from failures[0]


def is_registered() -> bool:
    """Root Shell과 세 도구가 등록 완료 상태인지 반환합니다."""

    return _REGISTERED


__all__ = ["is_registered", "register", "unregister"]
