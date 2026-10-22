"""공통 COLMAP 읽기 계층을 기존 Align 경로로 재노출합니다."""

from importlib import import_module
import sys
from types import ModuleType

# 설치된 확장 패키지와 소스 직접 실행에서 같은 공통 패키지를 선택합니다.
_root_package = __package__.rsplit(".tools.align.colmap_io", 1)[0] if ".tools.align.colmap_io" in __package__ else ""
_shared_io = import_module(f"{_root_package}.shared.colmap_io" if _root_package else "shared.colmap_io")
__all__ = _shared_io.__all__


class _CompatibilityModule(ModuleType):
    """기존 공개 심벌 교체도 공통 구현에 전달하는 호환 모듈입니다."""

    def __getattr__(self, name):
        """현재 패키지에 없는 심벌을 공통 구현에서 조회합니다."""
        return getattr(_shared_io, name)

    def __setattr__(self, name, value):
        """공개 함수 교체를 공통 구현에 전달하고 패키지 메타정보는 보존합니다."""
        if name in __all__ or name == "LOGGER":
            setattr(_shared_io, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        """임시 교체 심벌의 삭제와 복원을 공통 구현에 전달합니다."""
        if name in __all__ or name == "LOGGER":
            delattr(_shared_io, name)
        else:
            super().__delattr__(name)

    def __dir__(self):
        """기존 패키지의 심벌 탐색에 공통 공개 항목을 포함합니다."""
        return sorted(set(super().__dir__()) | set(dir(_shared_io)))


sys.modules[__name__].__class__ = _CompatibilityModule
