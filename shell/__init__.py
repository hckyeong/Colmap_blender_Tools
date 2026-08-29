"""통합 Extension의 공통 등록과 메뉴 계층을 제공합니다."""

from .lifecycle import is_registered, register, unregister

__all__ = ["is_registered", "register", "unregister"]
