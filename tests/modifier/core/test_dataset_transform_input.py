"""Blender 없이 전체 데이터 모달의 확정·취소 및 소유권 계약을 검증합니다."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[3] / "tools/modifier/blender/dataset_transform_gizmo.py"


def _method(name, namespace):
    """실제 제품 모달의 메서드를 분리하여 최소 대역으로 실행합니다."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "COLMAP_OT_transform_dataset")
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace[name]


class DatasetTransformInputTests(unittest.TestCase):
    """핸들의 놓기와 단축키의 누르기를 혼동하지 않도록 검증합니다."""

    def test_confirmation_edges_and_focus_cancellation(self):
        """핸들과 키보드 확정 시점 및 포커스 상실 취소를 검증합니다."""
        modal = _method("modal", {"_AXES": {"X", "Y", "Z"}})
        state = SimpleNamespace(from_gizmo=True, _awaiting_viewport=True, _current=lambda context: True,
                                _finish=lambda context, commit: {"COMMIT" if commit else "CANCEL"})
        for from_gizmo, edge in ((True, "RELEASE"), (False, "PRESS")):
            state.from_gizmo = from_gizmo
            self.assertEqual(modal(state, None, SimpleNamespace(type="LEFTMOUSE", value=edge)), {"COMMIT"})
            other = "PRESS" if edge == "RELEASE" else "RELEASE"
            self.assertEqual(modal(state, None, SimpleNamespace(type="LEFTMOUSE", value=other)), {"RUNNING_MODAL"})
        for event_type in ("ESC", "RIGHTMOUSE", "WINDOW_DEACTIVATE"):
            self.assertEqual(modal(state, None, SimpleNamespace(type=event_type, value="PRESS")), {"CANCEL"})
        state._current = lambda context: False
        self.assertEqual(modal(state, None, SimpleNamespace(type="RET", value="PRESS")), {"CANCEL"})

    def test_panel_first_viewport_motion_only_sets_anchor(self):
        """패널 클릭 좌표가 회전·배율 미리보기에 즉시 사용되지 않도록 검증합니다."""
        pointer = SimpleNamespace(x=120, y=30)
        update = _method("_update", {"_mouse": lambda region, event: pointer})
        state = SimpleNamespace(_region=SimpleNamespace(width=100, height=100),
                                _awaiting_viewport=True, _start_mouse=None)
        update(state, None)
        self.assertTrue(state._awaiting_viewport)
        self.assertIsNone(state._start_mouse)
        pointer.x = 80
        update(state, None)
        self.assertFalse(state._awaiting_viewport)
        self.assertIs(state._start_mouse, pointer)

    def test_release_does_not_restore_replacement_domain(self):
        """도메인이 교체되면 기존 잠금만 풀고 새 도메인 표시를 건드리지 않습니다."""
        calls = []
        original = SimpleNamespace(transform_preview_active=True)
        replacement = SimpleNamespace(transform_preview_active=True)
        state = SimpleNamespace(_scene=object(), _domain=original, _timer=object(),
                                _manager=SimpleNamespace(event_timer_remove=calls.append),
                                _area=SimpleNamespace(tag_redraw=lambda: calls.append("redraw")))
        namespace = {"_ACTIVE": state, "domain_for_scene": lambda scene: replacement,
                     "apply_dataset_transform_preview": lambda scene: calls.append("restore")}
        release = _method("_release", namespace)
        release(state, restore=True)
        self.assertFalse(original.transform_preview_active)
        self.assertTrue(replacement.transform_preview_active)
        self.assertIsNone(namespace["_ACTIVE"])
        self.assertNotIn("restore", calls)
        self.assertIsNone(state._timer)
        previous = list(calls)
        release(state, restore=True)
        self.assertEqual(calls, previous)


if __name__ == "__main__":
    unittest.main()
