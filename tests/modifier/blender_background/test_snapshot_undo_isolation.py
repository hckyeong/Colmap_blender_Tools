"""비활성 도구 Undo Hook이 다른 도구의 무거운 복구를 실행하지 않는지 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch

import bpy


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.modifier.blender import state_handlers as modifier_handlers  # noqa: E402
from tools.projection_paint.blender import state_handlers as paint_handlers  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    """검증 조건이 거짓이면 지정한 한국어 원인으로 실패합니다."""

    if not condition:
        raise AssertionError(message)


def main() -> None:
    """Painting Undo 상황에서 Modifier full reconcile과 비활성 PP rebuild가 0회인지 확인합니다."""

    with patch.object(modifier_handlers, "schedule_undo_domain_reconcile") as reconcile:
        modifier_handlers.reconcile_after_undo_redo()
    assert_true(reconcile.call_count == 0, "비활성 Modifier full reconcile이 실행됐습니다.")

    scene = bpy.context.scene
    with (
        patch.object(paint_handlers, "_projection_workspace_active", return_value=False),
        patch.object(paint_handlers, "_rebuild_transient_state") as rebuild,
    ):
        paint_handlers.undo_post_handler(scene)
    assert_true(rebuild.call_count == 0, "비활성 Projection Painting rebuild가 실행됐습니다.")
    assert_true(
        int(scene.as_pointer()) in paint_handlers._LAZY_RECOVERY_SCENES,
        "비활성 Projection Painting이 lazy recovery 상태로 기록되지 않았습니다.",
    )
    print("Modifier·Projection Painting Undo 격리 회귀 통과")


if __name__ == "__main__":
    main()
