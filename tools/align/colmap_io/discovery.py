"""기존 경로에서 공통 COLMAP 모듈과 동일한 객체를 제공합니다."""

import sys

from . import _shared_io

sys.modules[__name__] = getattr(_shared_io, "discovery")
