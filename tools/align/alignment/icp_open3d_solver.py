"""Full 개발 빌드의 Open3D solver 호환 import 경로입니다."""

from importlib import import_module
import sys

_IMPLEMENTATION = import_module("..optional_backends.open3d.solver", package=__package__)
sys.modules[__name__] = _IMPLEMENTATION
