"""Full 개발 빌드의 Hybrid worker 호환 진입점입니다."""

from importlib import import_module
from pathlib import Path
import sys

if __name__ == "__main__":
    addon_parent = str(Path(__file__).resolve().parents[2])
    if addon_parent not in sys.path:
        sys.path.insert(0, addon_parent)

_IMPLEMENTATION = import_module(
    "align.optional_backends.hybrid.worker"
    if __name__ == "__main__"
    else "..optional_backends.hybrid.worker",
    package=None if __name__ == "__main__" else __package__,
)

if __name__ == "__main__":
    raise SystemExit(_IMPLEMENTATION.main())

sys.modules[__name__] = _IMPLEMENTATION
