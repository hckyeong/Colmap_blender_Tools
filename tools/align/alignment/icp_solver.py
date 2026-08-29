"""Full 개발 빌드의 Hybrid solver 호환 import 경로입니다."""

from importlib import import_module
import sys

_IMPLEMENTATION = import_module("..optional_backends.hybrid.solver", package=__package__)
sys.modules[__name__] = _IMPLEMENTATION
